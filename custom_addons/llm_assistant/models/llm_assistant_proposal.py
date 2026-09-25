import json

from lxml import etree
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.models import MAGIC_COLUMNS, get_public_method
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import html2plaintext
from odoo.tools.json import json_default

from .llm_assistant_tools import MAX_TEXT_LENGTH, TOOL_ERRORS

MAX_PROPOSAL_RECORDS = 100
MAX_PREVIEW_RECORDS = 10
# Methods with a dedicated proposal type, which shows what they change.
ACTION_DENIED_METHODS = ('create', 'write', 'unlink')


class LlmAssistantProposal(models.Model):
    """A change the assistant wants to make, waiting for its user to confirm it.

    Proposals are created by the write tools of ``llm.assistant.tools`` with
    the rights of the user the assistant acts for, and only that user can
    confirm them. Confirming runs the change with that user's rights.
    """
    _name = 'llm.assistant.proposal'
    _description = "AI Assistant Proposal"
    _order = 'id desc'

    name = fields.Char(string="Summary", required=True, readonly=True)
    description = fields.Text(string="Reason", readonly=True, help="Why the assistant proposes this change.")
    user_id = fields.Many2one(
        'res.users', string="Requested By", required=True, readonly=True, index=True, ondelete='cascade',
        default=lambda self: self.env.user,
    )
    state = fields.Selection(
        [('pending', 'To Confirm'), ('done', 'Done'), ('rejected', 'Rejected'), ('failed', 'Failed')],
        required=True, readonly=True, default='pending',
    )
    operation = fields.Selection(
        [('create', 'Create'), ('write', 'Update'), ('unlink', 'Delete'), ('action', 'Run Action')],
        required=True, readonly=True,
    )
    res_model = fields.Char(string="Model", required=True, readonly=True)
    res_model_description = fields.Char(string="Document Type", compute='_compute_res_model_description')
    res_ids = fields.Json(string="Record IDs", readonly=True)
    values = fields.Json(readonly=True)
    method = fields.Char(readonly=True)
    preview = fields.Html(readonly=True)
    technical_details = fields.Text(compute='_compute_technical_details')
    error = fields.Text(readonly=True)
    source = fields.Selection(
        [('chat', 'Odoo Chat'), ('mcp', 'MCP Client')], required=True, readonly=True, default='chat',
    )
    channel_id = fields.Many2one('discuss.channel', readonly=True, index='btree_not_null', ondelete='set null')
    job_id = fields.Many2one('llm.assistant.job', readonly=True, index='btree_not_null', ondelete='set null')
    handled_date = fields.Datetime(string="Handled On", readonly=True)

    @api.depends('res_model')
    def _compute_res_model_description(self):
        for proposal in self:
            if proposal.res_model in self.env:
                proposal.res_model_description = self.env[proposal.res_model]._description
            else:
                proposal.res_model_description = proposal.res_model

    @api.depends('res_ids', 'values', 'method')
    def _compute_technical_details(self):
        for proposal in self:
            details = {'model': proposal.res_model, 'operation': proposal.operation}
            if proposal.res_ids:
                details['ids'] = proposal.res_ids
            if proposal.method:
                details['method'] = proposal.method
            if proposal.values:
                details['values'] = proposal.values
            proposal.technical_details = json.dumps(details, indent=2, ensure_ascii=False, default=json_default)

    # ------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------

    def action_confirm(self):
        """Apply the proposed change with the rights of the current user."""
        self.ensure_one()
        self._check_can_handle()
        try:
            with self.env.cr.savepoint():
                records, action = self._execute()
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            # not a failure of the change: the request is retried
            raise
        except TOOL_ERRORS as error:
            self._set_handled('failed', error=self.env['llm.assistant.tools']._format_error(error))
            self._notify_channel()
            return False
        self._set_handled('done', res_ids=records.ids)
        self._notify_channel()
        # a button can open a follow-up screen (a wizard, a report, ...)
        if isinstance(action, dict) and str(action.get('type', '')).startswith('ir.actions.'):
            return action
        return False

    def action_reject(self):
        self.ensure_one()
        self._check_can_handle()
        self._set_handled('rejected')
        self._notify_channel()

    def action_open_records(self):
        self.ensure_one()
        records = self.env[self.res_model].browse(self.res_ids or []).exists()
        if not records:
            raise UserError(self.env._("The records of this proposal no longer exist."))
        action = {
            'type': 'ir.actions.act_window',
            'name': self.res_model_description,
            'res_model': self.res_model,
        }
        if len(records) == 1:
            return {**action, 'res_id': records.id, 'views': [(False, 'form')]}
        return {**action, 'domain': [('id', 'in', records.ids)], 'views': [(False, 'list'), (False, 'form')]}

    # ------------------------------------------------------------
    # Business methods
    # ------------------------------------------------------------

    @api.model
    def _propose(self, operation, model_name, ids=None, values=None, method=None, summary=None):
        """Create a proposal after checking the request as far as possible
        without changing anything, so that the LLM learns about mistakes
        before the user sees them.

        The source (chat or MCP) and the chat turn come from the context keys
        ``llm_assistant_source`` and ``llm_assistant_job_id``.
        """
        Tools = self.env['llm.assistant.tools']
        Model = Tools._get_model(model_name, 'write')
        records = Model.browse()
        if operation == 'create':
            Model.check_access('create')
            values = self._check_values(Model, values)
            self._check_required_values(Model, values)
        else:
            records = self._get_records(Model, ids)
            if operation == 'write':
                values = self._check_values(Model, values)
                records.check_access('write')
            elif operation == 'unlink':
                records.check_access('unlink')
            elif operation == 'action':
                if not isinstance(method, str) or not method or method in ACTION_DENIED_METHODS:
                    raise UserError(self.env._(
                        "Give the method name of a button, such as 'action_confirm'. Use propose_create, "
                        "propose_update or propose_delete to create, change or delete records.",
                    ))
                get_public_method(Model, method)
            else:
                raise UserError(self.env._("Unknown operation '%s'.", operation))

        job = self.env['llm.assistant.job'].browse(self.env.context.get('llm_assistant_job_id'))
        return self.create({
            'name': self._get_summary(operation, Model, records, method),
            'description': str(summary)[:MAX_TEXT_LENGTH] if summary else False,
            'operation': operation,
            'res_model': Model._name,
            'res_ids': records.ids or False,
            'values': values or False,
            'method': method if operation == 'action' else False,
            'preview': self._build_preview(operation, Model, records, values, method),
            'source': self.env.context.get('llm_assistant_source') or 'chat',
            'job_id': job.id,
            'channel_id': job.channel_id.id,
        })

    @api.model
    def _get_records(self, Model, ids):
        if isinstance(ids, (int, str)):
            ids = [ids]
        try:
            ids = [int(id_) for id_ in ids] if isinstance(ids, list) else []
        except ValueError:
            ids = []
        if not ids:
            raise UserError(self.env._("Give the IDs of the records as a list of integers."))
        if len(ids) > MAX_PROPOSAL_RECORDS:
            raise UserError(self.env._("A proposal can change at most %s records.", MAX_PROPOSAL_RECORDS))
        records = Model.browse(id_ for id_ in ids if id_ > 0).exists()
        if missing := sorted(set(ids) - set(records.ids)):
            raise UserError(self.env._("No %(model)s with IDs %(ids)s.", model=Model._name, ids=missing))
        records.check_access('read')
        return records

    @api.model
    def _check_values(self, Model, values):
        if not isinstance(values, dict) or not values:
            raise UserError(self.env._("Give the field values as a JSON object, e.g. {\"name\": \"New name\"}."))
        Tools = self.env['llm.assistant.tools']
        if unknown := [name for name in values if name not in Model._fields]:
            raise UserError(self.env._(
                "Unknown fields on %(model)s: %(fields)s. Use get_fields to list them.",
                model=Model._name, fields=unknown,
            ))
        for name in values:
            field = Model._fields[name]
            if name in MAGIC_COLUMNS or name == 'display_name' or Tools._is_secret_field(name):
                raise UserError(self.env._("Field '%s' cannot be set by the assistant.", name))
            if field.compute and field.readonly:
                raise UserError(self.env._("Field '%s' is computed and cannot be set.", name))
            Model.check_field_access(field, 'write')
            if field.type in ('one2many', 'many2many'):
                self._check_x2many_value(Model.env[field.comodel_name], field, values[name])
        return values

    @api.model
    def _check_x2many_value(self, Comodel, field, commands):
        """Check the lines an x2many value creates or changes like top-level
        values, so that no change reaches a model the assistant cannot change."""
        if self.env['llm.assistant.tools']._is_model_denied(Comodel._name, 'write'):
            raise UserError(self.env._(
                "Field '%(field)s' links to %(model)s, which the assistant cannot change.",
                field=field.name, model=Comodel._name,
            ))
        if not isinstance(commands, list):
            raise UserError(self.env._("Give field '%s' as a list of IDs or of commands.", field.name))
        for command in commands:
            if isinstance(command, (list, tuple)) and len(command) > 2 and command[0] in (0, 1):
                self._check_values(Comodel, command[2])

    @api.model
    def _check_required_values(self, Model, values):
        """Refuse a creation that misses required values without defaults."""
        candidates = [
            name for name, field in Model._fields.items()
            if field.required and not field.compute and not field.related and name not in values
            and name not in MAGIC_COLUMNS
        ]
        defaults = Model.default_get(candidates) if candidates else {}
        if missing := [name for name in candidates if name not in defaults]:
            raise UserError(self.env._(
                "Missing required fields for %(model)s: %(fields)s.", model=Model._name, fields=missing,
            ))

    @api.model
    def _get_summary(self, operation, Model, records, method):
        document = Model._description
        match operation:
            case 'create':
                return self.env._("Create %s", document)
            case 'write':
                return self.env._("Update %(count)s x %(document)s", count=len(records), document=document)
            case 'unlink':
                return self.env._("Delete %(count)s x %(document)s", count=len(records), document=document)
            case _:
                return self.env._(
                    "%(button)s on %(count)s x %(document)s",
                    button=self._get_button_label(Model, method), count=len(records), document=document,
                )

    @api.model
    def _get_button_label(self, Model, method):
        """Return the label of the form view button calling ``method``, or the method name."""
        try:
            arch = Model.get_view(view_type='form')['arch']
        except TOOL_ERRORS:
            return method
        for button in etree.fromstring(arch).iter('button'):
            if button.get('name') == method and button.get('type') == 'object' and button.get('string'):
                return button.get('string')
        return method

    @api.model
    def _build_preview(self, operation, Model, records, values, method):
        """Describe the change for the user who confirms it (HTML, escaped)."""
        parts = []
        if records:
            names = Markup().join(
                Markup("<li>%s</li>") % record.display_name for record in records[:MAX_PREVIEW_RECORDS]
            )
            if len(records) > MAX_PREVIEW_RECORDS:
                names += Markup("<li>%s</li>") % self.env._("and %s more", len(records) - MAX_PREVIEW_RECORDS)
            parts.append(Markup("<p>%s</p><ul>%s</ul>") % (self.env._("Records:"), names))
        if operation == 'unlink':
            parts.append(Markup("<p><strong>%s</strong></p>") % self.env._("These records will be deleted."))
        elif operation == 'action':
            parts.append(Markup("<p>%s <strong>%s</strong> (<code>%s</code>)</p>") % (
                self.env._("Button:"), self._get_button_label(Model, method), method,
            ))
        if values:
            show_current = operation == 'write'
            rows = Markup()
            for name, value in values.items():
                field = Model._fields[name]
                cells = Markup("<td>%s</td>") % field._description_string(self.env)
                if show_current:
                    current = {self._format_current_value(record, field) for record in records}
                    cells += Markup("<td>%s</td>") % (current.pop() if len(current) == 1 else self.env._("(varies)"))
                cells += Markup("<td><strong>%s</strong></td>") % self._format_new_value(Model, field, value)
                rows += Markup("<tr>%s</tr>") % cells
            header = Markup("<th>%s</th>") % self.env._("Field")
            if show_current:
                header += Markup("<th>%s</th>") % self.env._("Current Value")
            header += Markup("<th>%s</th>") % self.env._("New Value")
            parts.append(Markup(
                '<table class="table table-sm table-bordered"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>',
            ) % (header, rows))
        return Markup().join(parts)

    @api.model
    def _format_current_value(self, record, field):
        value = record[field.name]
        if field.type == 'many2one':
            return value.display_name or ''
        if field.type in ('one2many', 'many2many'):
            return self._format_names(value)
        return self._format_scalar(field, value)

    @api.model
    def _format_new_value(self, Model, field, value):
        if field.type == 'many2one':
            if not value:
                return ''
            return self._format_names(self._browse_readable(field.comodel_name, [value])) or f"#{value}"
        if field.type in ('one2many', 'many2many'):
            return self._format_commands(Model.env[field.comodel_name], value)
        return self._format_scalar(field, value)

    @api.model
    def _format_scalar(self, field, value):
        if field.type == 'boolean':
            return self.env._("Yes") if value else self.env._("No")
        if value is False or value is None:
            return ''
        if field.type == 'selection':
            return dict(field._description_selection(self.env)).get(value, value)
        if field.type == 'html':
            value = html2plaintext(value, include_references=False)
        return str(value)[:MAX_TEXT_LENGTH]

    @api.model
    def _format_names(self, records):
        names = records[:MAX_PREVIEW_RECORDS].mapped('display_name')
        if len(records) > MAX_PREVIEW_RECORDS:
            names.append(self.env._("and %s more", len(records) - MAX_PREVIEW_RECORDS))
        return ", ".join(names)

    @api.model
    def _browse_readable(self, model_name, ids):
        records = self.env[model_name].browse(id_ for id_ in ids if isinstance(id_, int) and id_ > 0).exists()
        return records._filtered_access('read')

    @api.model
    def _format_commands(self, Comodel, commands):
        """Describe an x2many value: a list of IDs or of ORM commands."""
        if not isinstance(commands, list):
            return str(commands)[:MAX_TEXT_LENGTH]
        if all(isinstance(command, int) for command in commands):
            return self._format_names(self._browse_readable(Comodel._name, commands))
        lines = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                lines.append(str(command))
                continue
            match command[0]:
                case 0 if len(command) > 2 and isinstance(command[2], dict):
                    lines.append(self.env._("New: %s", self._format_line_values(Comodel, command[2])))
                case 1 if len(command) > 2 and isinstance(command[2], dict):
                    lines.append(self.env._("Change %(record)s: %(values)s",
                        record=self._format_names(self._browse_readable(Comodel._name, [command[1]])),
                        values=self._format_line_values(Comodel, command[2])))
                case 2 | 3:
                    lines.append(self.env._("Remove %s",
                        self._format_names(self._browse_readable(Comodel._name, [command[1]]))))
                case 4:
                    lines.append(self.env._("Add %s",
                        self._format_names(self._browse_readable(Comodel._name, [command[1]]))))
                case 5:
                    lines.append(self.env._("Remove all"))
                case 6 if len(command) > 2 and isinstance(command[2], list):
                    lines.append(self.env._("Set to %s",
                        self._format_names(self._browse_readable(Comodel._name, command[2]))))
                case _:
                    lines.append(str(command)[:MAX_TEXT_LENGTH])
        return "; ".join(lines)

    @api.model
    def _format_line_values(self, Comodel, values):
        formatted = []
        for name, value in values.items():
            field = Comodel._fields.get(name)
            if field is None:
                formatted.append(f"{name}: {value}")
            elif field.type in ('one2many', 'many2many'):
                formatted.append(f"{field._description_string(self.env)}: {self._format_commands(Comodel.env[field.comodel_name], value)}")
            else:
                formatted.append(f"{field._description_string(self.env)}: {self._format_new_value(Comodel, field, value)}")
        return ", ".join(formatted)

    def _check_can_handle(self):
        self.ensure_one()
        if self.user_id != self.env.user:
            raise AccessError(self.env._("Only %s can confirm or reject this proposal.", self.user_id.name))
        if self.state != 'pending':
            raise UserError(self.env._("This proposal was already handled."))

    def _execute(self):
        """Apply the change as the current user.

        :return: a pair (records, action): the created or changed records, and
            what the button returned for an action.
        """
        self.ensure_one()
        # checks again, in case the settings changed since the proposal
        Model = self.env['llm.assistant.tools']._get_model(self.res_model, 'write')
        records = Model.browse(self.res_ids or [])
        action = None
        match self.operation:
            case 'create':
                records = Model.create(self.values)
            case 'write':
                records.write(self.values)
            case 'unlink':
                records.unlink()
            case 'action':
                action = get_public_method(Model, self.method)(records)
        return records, action

    def _set_handled(self, state, res_ids=None, error=False):
        vals = {'state': state, 'handled_date': fields.Datetime.now(), 'error': error}
        if res_ids is not None and self.operation == 'create':
            vals['res_ids'] = res_ids
        # sudo: users can read but not write their proposals, so that what
        # they confirmed stays as it was proposed
        self.sudo().write(vals)

    def _notify_channel(self):
        """Tell the chat the proposal came from what happened to it."""
        self.ensure_one()
        if not self.channel_id:
            return
        match self.state:
            case 'done':
                body = Markup("%s <a href='%s'>%s</a>") % (self.env._("Done:"), self._get_url(), self.name)
            case 'rejected':
                body = Markup("%s <a href='%s'>%s</a>") % (self.env._("Rejected:"), self._get_url(), self.name)
            case 'failed':
                body = Markup("%s <a href='%s'>%s</a><br/>%s") % (
                    self.env._("Failed:"), self._get_url(), self.name, self.error,
                )
            case _:
                return
        self.channel_id._llm_assistant_post(body)

    def _get_url(self):
        self.ensure_one()
        return f"{self.get_base_url()}/odoo/action-llm_assistant.llm_assistant_proposal_action/{self.id}"

    def _get_tool_result(self):
        """What a tool returns to the LLM about this proposal."""
        self.ensure_one()
        result = {
            'proposal_id': self.id,
            'status': self.state,
            'summary': self.name,
            'url': self._get_url(),
        }
        if self.state == 'pending':
            result['message'] = (
                "Nothing has changed yet. The user must open the link and confirm the proposal in Odoo."
            )
        if self.state == 'done' and self.res_ids:
            result['record_ids'] = self.res_ids
        if self.error:
            result['error'] = self.error
        return result
