import json
from unittest.mock import patch

import psycopg2
from markupsafe import Markup

from odoo.tests import tagged

from .common import LlmAssistantCommon
from odoo.addons.llm_assistant.models.llm_assistant_client import (
    LlmAssistantConnectionError,
)


def tool_call(call_id, name, **arguments):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


@tagged('post_install', '-at_install')
class TestLlmAssistantChat(LlmAssistantCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].set_str('llm_assistant.model', 'test-model')
        cls.assistant = cls.env.ref('llm_assistant.partner_assistant')
        cls.channel = cls.env['discuss.channel'].with_user(cls.user)._get_or_create_chat([cls.assistant.id])

    def post(self, text, channel=None, user=None):
        return (channel or self.channel).with_user(user or self.user).message_post(
            body=text, message_type='comment', subtype_xmlid='mail.mt_comment',
        )

    def answer(self, replies):
        """Run the queued answers, the LLM giving ``replies`` in turn; return
        the mocked LLM call."""
        Client = type(self.env['llm.assistant.client'])
        with patch.object(Client, '_chat_completion', side_effect=replies) as chat_completion:
            for _step in range(20):
                job = self.queued_jobs()[:1]
                if not job:
                    break
                job._process_step()
        return chat_completion

    def queued_jobs(self):
        return self.env['llm.assistant.job'].search([('channel_id', '=', self.channel.id), ('state', '=', 'queued')], order='id')

    def last_message(self):
        return self.env['mail.message'].search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', self.channel.id)], order='id desc', limit=1,
        )

    def test_open_chat(self):
        action = self.env['discuss.channel'].with_user(self.user).action_llm_assistant_open_chat()
        self.assertEqual(action['url'], f'/odoo/action-mail.action_discuss?active_id={self.channel.id}')

    def test_answer_with_tool_call(self):
        self.post("What is the phone number of Azure Interior?")
        job = self.env['llm.assistant.job'].search([('channel_id', '=', self.channel.id)])
        self.assertRecordValues(job, [{'user_id': self.user.id, 'state': 'queued'}])

        chat_completion = self.answer([
            {'content': '', 'tool_calls': [
                tool_call('call_1', 'search_records', model='res.partner', domain=[['id', '=', self.partner.id]], fields=['phone']),
            ]},
            {'content': '<think>The tool gave the number.</think>It is **+1 555 0100**.'},
        ])
        self.assertRecordValues(job, [{'state': 'done', 'step': 1}])
        first_messages, tools = chat_completion.call_args_list[0].args[:2]
        self.assertEqual([message['role'] for message in first_messages], ['system', 'user'])
        self.assertIn('Ann User', first_messages[0]['content'])
        self.assertEqual(first_messages[1]['content'], "What is the phone number of Azure Interior?")
        self.assertIn('search_records', [tool['function']['name'] for tool in tools])
        second_messages = chat_completion.call_args_list[1].args[0]
        self.assertEqual(second_messages[-1]['role'], 'tool')
        self.assertEqual(second_messages[-1]['tool_call_id'], 'call_1')
        self.assertIn('+1 555 0100', second_messages[-1]['content'])

        reply = self.last_message()
        self.assertEqual(reply.author_id, self.assistant)
        self.assertIn('<strong>+1 555 0100</strong>', reply.body)
        self.assertNotIn('think', reply.body)
        self.assertFalse(self.queued_jobs(), "the reply is not answered")

    def test_answer_with_proposal(self):
        self.post("Change the phone of Azure Interior to +1 555 0199")
        self.answer([
            {'content': '', 'tool_calls': [
                tool_call('call_1', 'propose_update', model='res.partner', ids=[self.partner.id], values={'phone': '+1 555 0199'}),
            ]},
            {'content': 'I proposed the change, please confirm it.'},
        ])
        proposal = self.env['llm.assistant.proposal'].search([('user_id', '=', self.user.id)])
        self.assertRecordValues(proposal, [{'state': 'pending', 'source': 'chat', 'channel_id': self.channel.id}])
        self.assertIn(f'llm_assistant_proposal_action/{proposal.id}', self.last_message().body)
        self.assertEqual(self.partner.phone, '+1 555 0100')

        proposal.with_user(self.user).action_confirm()
        self.assertEqual(self.partner.phone, '+1 555 0199')
        self.assertIn('Done:', self.last_message().body)
        self.assertFalse(self.queued_jobs(), "the assistant's notes are not answered")

    def test_follow_up_uses_history(self):
        self.post("Hello")
        self.answer([{'content': 'Hi Ann, how can I help?'}])
        self.post("Who works at Azure Interior?")
        chat_completion = self.answer([{'content': 'Brandon Freeman.'}])
        messages = chat_completion.call_args.args[0]
        self.assertEqual(
            [(message['role'], message['content']) for message in messages[1:]],
            [('user', 'Hello'), ('assistant', 'Hi Ann, how can I help?'), ('user', 'Who works at Azure Interior?')],
        )

    def test_messages_in_a_row_get_one_answer(self):
        self.post("Hello")
        self.post("Who works at Azure Interior?")
        self.assertEqual(len(self.queued_jobs()), 2)
        chat_completion = self.answer([{'content': 'Brandon Freeman.'}])
        self.assertEqual(chat_completion.call_count, 1, "the first answer read both messages")
        messages = chat_completion.call_args.args[0]
        self.assertEqual(messages[1:], [{'role': 'user', 'content': "Hello\n\nWho works at Azure Interior?"}])
        replies = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', self.channel.id), ('author_id', '=', self.assistant.id)])
        self.assertEqual(len(replies), 1)
        self.assertFalse(self.queued_jobs())

    def test_message_during_an_answer_gets_its_own_answer(self):
        self.post("Count the contacts")
        Client = type(self.env['llm.assistant.client'])
        count = {'content': '', 'tool_calls': [tool_call('call_1', 'count_records', model='res.partner')]}
        with patch.object(Client, '_chat_completion', return_value=count):
            self.queued_jobs()._process_step()
        # the answer has started when the user writes again
        self.post("And the companies?")
        chat_completion = self.answer([{'content': 'There are many contacts.'}, {'content': 'And some companies.'}])
        self.assertEqual(chat_completion.call_count, 2)
        second_answer_messages = chat_completion.call_args.args[0]
        self.assertEqual(
            [(message['role'], message['content']) for message in second_answer_messages[1:]],
            [('user', 'Count the contacts'), ('assistant', 'There are many contacts.'), ('user', 'And the companies?')],
        )
        self.assertEqual(self.last_message().body, '<p>And some companies.</p>')

    def test_history_without_earlier_answers(self):
        self.post("Hello")
        self.queued_jobs().unlink()  # as if the chat started before the assistant answered through jobs
        self.channel._llm_assistant_post(Markup("<p>Hi Ann</p>"))
        self.post("Who works at Azure Interior?")
        chat_completion = self.answer([{'content': 'Brandon Freeman.'}])
        self.assertEqual(
            [(message['role'], message['content']) for message in chat_completion.call_args.args[0][1:]],
            [('user', 'Hello'), ('assistant', 'Hi Ann'), ('user', 'Who works at Azure Interior?')],
        )

    def test_jobs_of_a_chat_run_in_order(self):
        other_channel = self.env['discuss.channel'].with_user(self.other_user)._get_or_create_chat([self.assistant.id])
        self.post("First")
        self.post("Second", channel=other_channel, user=self.other_user)
        self.post("Third")
        Job = self.env['llm.assistant.job']
        first, second, third = Job.search([('channel_id', 'in', (self.channel | other_channel).ids)], order='id')
        self.assertEqual(Job._get_next_job(), first)
        first.state = 'done'
        self.assertEqual(Job._get_next_job(), second, "the third job waits for the first one of its chat")
        second.state = 'done'
        self.assertEqual(Job._get_next_job(), third)

    def test_step_limit(self):
        self.env['ir.config_parameter'].set_int('llm_assistant.max_steps', 1)
        self.post("Count the contacts")
        count = {'content': '', 'tool_calls': [tool_call('call_1', 'count_records', model='res.partner')]}
        chat_completion = self.answer([count, count])
        self.assertEqual(chat_completion.call_count, 2)
        self.assertIn('I stopped after 1 steps', self.last_message().body)

    def test_invalid_tool_arguments_are_reported(self):
        self.post("Search")
        chat_completion = self.answer([
            {'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'search_records', 'arguments': '{not json'}}]},
            {'content': 'Sorry, something went wrong.'},
        ])
        tool_message = chat_completion.call_args.args[0][-1]
        self.assertIn('JSON object', tool_message['content'])

    def test_connection_error(self):
        self.post("Hello")
        self.answer(LlmAssistantConnectionError("Could not reach the AI server"))
        job = self.env['llm.assistant.job'].search([('channel_id', '=', self.channel.id)])
        self.assertRecordValues(job, [{'state': 'failed', 'error': "Could not reach the AI server"}])
        self.assertIn('Sorry, I could not answer', self.last_message().body)

    def test_concurrent_update_is_retried(self):
        self.post("Hello")
        Job = type(self.env['llm.assistant.job'])
        conflict = psycopg2.errors.SerializationFailure("could not serialize access due to concurrent update")
        with patch.object(Job, '_run_step', side_effect=conflict), self.assertRaises(psycopg2.errors.SerializationFailure):
            self.queued_jobs()._process_step()
        self.assertEqual(len(self.queued_jobs()), 1, "the answer is still to do")

    def test_other_conversations_are_ignored(self):
        other_chat = self.env['discuss.channel'].with_user(self.user)._get_or_create_chat([self.other_user.partner_id.id])
        self.post("Hello Bob", channel=other_chat)
        group = self.env['discuss.channel'].with_user(self.user)._create_group(users_to=self.other_user)
        group.sudo()._add_members(partners=self.assistant, post_joined_message=False)
        self.post("Hello all", channel=group)
        self.assertFalse(self.env['llm.assistant.job'].search([('channel_id', 'in', (other_chat | group).ids)]))
