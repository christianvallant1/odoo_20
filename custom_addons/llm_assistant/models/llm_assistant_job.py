import json
import logging
import re

from markupsafe import Markup

from odoo import api, fields, models
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import html2plaintext, plaintext2html
from odoo.tools.json import json_default

from .llm_assistant_client import LlmAssistantConnectionError
from odoo.addons.llm_assistant.tools import markdown_to_html

_logger = logging.getLogger(__name__)

HISTORY_LIMIT = 20
MAX_CRON_ITERATIONS = 100
# reasoning models may think out loud before answering
THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)


class LlmAssistantJob(models.Model):
    """One answer of the assistant in a Discuss chat.

    Each message posted to the assistant queues a job, and a cron answers it
    one step at a time: a step is one call to the LLM, then the tool calls it
    asked for. The transcript is saved after each step, so a long answer
    survives cron time limits and restarts.

    The jobs of a chat run in order, and a job starts by reading the chat
    history: when an earlier job already read its message (the user sent
    several messages in a row), the job is done without answering again.
    Posting a message only ever creates a job, so it never competes with the
    cron over the same rows.
    """
    _name = 'llm.assistant.job'
    _description = "AI Assistant Answer"
    _order = 'id desc'

    channel_id = fields.Many2one(
        'discuss.channel', string="Chat", required=True, readonly=True, index=True, ondelete='cascade',
    )
    user_id = fields.Many2one('res.users', required=True, readonly=True, index=True, ondelete='cascade')
    message_id = fields.Many2one('mail.message', string="Question", readonly=True, ondelete='set null')
    last_message_id = fields.Many2one(
        'mail.message', string="Last Message Read", readonly=True, ondelete='set null',
        help="The most recent chat message the answer took into account.",
    )
    state = fields.Selection(
        [('queued', 'Queued'), ('done', 'Done'), ('failed', 'Failed')],
        required=True, readonly=True, default='queued',
    )
    step = fields.Integer(readonly=True)
    messages = fields.Json(string="Messages", readonly=True)
    transcript = fields.Text(compute='_compute_transcript')
    error = fields.Text(readonly=True)
    proposal_ids = fields.One2many('llm.assistant.proposal', 'job_id', string="Proposals", readonly=True)

    @api.depends('messages')
    def _compute_transcript(self):
        for job in self:
            job.transcript = json.dumps(job.messages or [], indent=2, ensure_ascii=False, default=json_default)

    # ------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------

    @api.model
    def _enqueue(self, channel, user, message):
        """Queue an answer to ``message``, posted by ``user`` in ``channel``."""
        job = self.create({'channel_id': channel.id, 'user_id': user.id, 'message_id': message.id})
        channel._llm_assistant_notify_typing(True)
        self.env.ref('llm_assistant.ir_cron_llm_assistant_jobs')._trigger()
        return job

    @api.model
    def _get_next_job(self):
        """Return the job to work on: the jobs of a chat run in order, and
        between chats, the job that waited the longest goes first, so that
        long answers take turns."""
        first_ids = [job_id for _channel, job_id in self._read_group([('state', '=', 'queued')], ['channel_id'], ['id:min'])]
        return self.search([('id', 'in', first_ids)], order='write_date, id', limit=1)

    @api.model
    def _cron_process_jobs(self):
        timeout = self.env['llm.assistant.client']._get_config()['timeout']
        IrCron = self.env['ir.cron']
        for _iteration in range(MAX_CRON_ITERATIONS):
            job = self._get_next_job()
            if not job:
                return
            job.channel_id._llm_assistant_notify_typing(True)
            IrCron._commit_progress(0)  # show that the assistant is typing
            try:
                job._process_step()
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                # the user wrote in the chat while the answer was posted:
                # undo the step and run it again
                IrCron._rollback_progress()
                continue
            remaining_time = IrCron._commit_progress(1, remaining=self.search_count([('state', '=', 'queued')]))
            if remaining_time < timeout:
                break
        # jobs are left: continue in another run
        self.env.ref('llm_assistant.ir_cron_llm_assistant_jobs')._trigger()

    def _process_step(self):
        """Run one step of the answer; on failure, tell the user in the chat."""
        self.ensure_one()
        job = self.with_context(lang=self.user_id.lang)
        try:
            with self.env.cr.savepoint():
                job._run_step()
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            # not a failure of the answer: the caller retries the step
            raise
        except LlmAssistantConnectionError as error:
            job._fail(error.args[0])
        except Exception as error:  # noqa: BLE001
            # whatever went wrong, the job must leave the queue and the user
            # must get an answer; the traceback goes to the server log
            _logger.exception("AI Assistant: answer %s failed", self.id)
            job._fail(str(error))

    # ------------------------------------------------------------
    # Answering
    # ------------------------------------------------------------

    def _run_step(self):
        self.ensure_one()
        Client = self.env['llm.assistant.client']
        config = Client._get_config()
        Tools = self._get_user_tools()
        messages = self.messages
        if not messages:
            if self._is_answered():
                self.write({'state': 'done'})
                self.channel_id._llm_assistant_notify_typing(False)
                return
            messages = self._prepare_messages(config)
        reply = self._normalize_reply(Client._chat_completion(messages, Tools._get_openai_tools(), config))
        messages = [*messages, reply]
        tool_calls = reply.get('tool_calls', [])
        if tool_calls and self.step < config['max_steps']:
            for call in tool_calls:
                result = Tools._execute_tool(call['function']['name'], self._parse_arguments(call['function']['arguments']))
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'content': json.dumps(result, ensure_ascii=False, default=json_default),
                })
            self.write({'messages': messages, 'step': self.step + 1})
            return
        answer = THINK_RE.sub('', reply['content']).strip()
        if tool_calls:
            answer = "\n\n".join(filter(None, [answer, self.env._(
                "I stopped after %s steps without finishing. Try a more specific question.", config['max_steps'],
            )]))
        self.write({'messages': messages, 'state': 'done'})
        self.channel_id._llm_assistant_post(self._format_answer(answer or self.env._("I have no answer to that.")))

    def _get_user_tools(self):
        """The tools, running with the rights of the user who asked."""
        self.ensure_one()
        user = self.user_id
        context = {
            'lang': user.lang,
            'tz': user.tz,
            'allowed_company_ids': [user.company_id.id, *(user.company_ids - user.company_id).ids],
            'llm_assistant_source': 'chat',
            'llm_assistant_job_id': self.id,
        }
        return self.env(user=user.id, su=False, context=context)['llm.assistant.tools']

    def _is_answered(self):
        """Whether an earlier answer in the chat already read this job's message."""
        self.ensure_one()
        return bool(self.message_id) and bool(self.search_count([
            ('channel_id', '=', self.channel_id.id),
            ('id', '!=', self.id),
            ('last_message_id', '>=', self.message_id.id),
        ], limit=1))

    def _prepare_messages(self, config):
        """Start the transcript: the system prompt, then the chat history."""
        self.ensure_one()
        assistant = self.env.ref('llm_assistant.partner_assistant')
        history = self.env['mail.message'].search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', self.channel_id.id), ('message_type', '=', 'comment')],
            order='id desc', limit=HISTORY_LIMIT,
        )
        self.last_message_id = history[:1]
        # The user may have written again while the previous answer was being
        # prepared; the messages no answer has read yet go last, so that the
        # transcript ends with what this answer must reply to.
        previous = self.search([
            ('channel_id', '=', self.channel_id.id), ('id', '<', self.id), ('last_message_id', '!=', False),
        ], order='id desc', limit=1)
        # without an earlier answer, what the assistant last said was read
        read_id = previous.last_message_id.id or max(history.filtered(lambda m: m.author_id == assistant).ids, default=0)
        unread = history.filtered(lambda m: m.id > read_id and m.author_id != assistant)
        messages = [{'role': 'system', 'content': self._get_system_prompt(config)}]
        for message in [*reversed(history - unread), *reversed(unread)]:
            role = 'assistant' if message.author_id == assistant else 'user'
            text = self._message_to_text(message)
            if not text or (role == 'assistant' and len(messages) == 1):
                # some chat templates require the conversation to start with the user
                continue
            if messages[-1]['role'] == role:
                # and roles to alternate
                messages[-1]['content'] += f"\n\n{text}"
            else:
                messages.append({'role': role, 'content': text})
        return messages

    def _get_system_prompt(self, config):
        self.ensure_one()
        user = self.user_id
        today = fields.Date.context_today(self.with_context(tz=user.tz))
        prompt = (
            f"You are the AI assistant built into Odoo, the business software of {user.company_id.name}. "
            f"You are chatting with {user.name}. Today is {today}. Answer in the language of the user's "
            "last message, briefly, in plain text or simple Markdown.\n\n"
            f"{self.env['llm.assistant.tools']._get_instructions()}"
        )
        if config['instructions']:
            prompt += f"\n\nInstructions from the administrator:\n{config['instructions']}"
        return prompt

    @api.model
    def _message_to_text(self, message):
        text = html2plaintext(message.body or '', include_references=False).strip()
        if message.attachment_ids:
            text += "\n" + self.env._("(Attached files the assistant cannot read: %s)", message.attachment_ids.mapped('name'))
        return text.strip()

    def _normalize_reply(self, message):
        """Make the LLM reply a valid transcript message: text content, and
        tool calls with IDs and JSON-encoded arguments."""
        self.ensure_one()
        tool_calls = []
        for index, call in enumerate(message.get('tool_calls') or []):
            function = call.get('function') or {}
            arguments = function.get('arguments')
            tool_calls.append({
                'id': call.get('id') or f'call_{self.step}_{index}',
                'type': 'function',
                'function': {
                    'name': function.get('name') or '',
                    'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                },
            })
        content = message.get('content')
        reply = {'role': 'assistant', 'content': content if isinstance(content, str) else ''}
        if tool_calls:
            reply['tool_calls'] = tool_calls
        return reply

    @api.model
    def _parse_arguments(self, arguments):
        if not arguments.strip():
            return {}
        try:
            return json.loads(arguments)
        except ValueError:
            # returned as is: the tool reports that it expects a JSON object
            return arguments

    def _format_answer(self, answer):
        """Turn the LLM answer into the HTML body of a chat message, with links
        to the proposals waiting for the user."""
        self.ensure_one()
        body = markdown_to_html(answer)
        if pending := self.proposal_ids.filtered(lambda proposal: proposal.state == 'pending'):
            links = Markup().join(
                Markup("<li><a href='%s'>%s</a></li>") % (proposal._get_url(), proposal.name) for proposal in pending
            )
            body += Markup("<p>%s</p><ul>%s</ul>") % (self.env._("Waiting for your confirmation:"), links)
        return body

    def _fail(self, error):
        self.ensure_one()
        self.write({'state': 'failed', 'error': error})
        self.channel_id._llm_assistant_post(plaintext2html(self.env._("Sorry, I could not answer: %s", error)))
