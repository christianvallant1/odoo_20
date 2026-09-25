from odoo import fields, models

from .llm_assistant_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    LlmAssistantConnectionError,
)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    llm_assistant_base_url = fields.Char(
        string="AI Server URL", config_parameter='llm_assistant.base_url', default=DEFAULT_BASE_URL,
        help="Base URL of an OpenAI-compatible API, ending before /chat/completions.",
    )
    llm_assistant_model = fields.Char(string="AI Model", config_parameter='llm_assistant.model')
    llm_assistant_api_key = fields.Char(
        string="AI Server API Key", config_parameter='llm_assistant.api_key',
        help="Only needed if the AI server requires one.",
    )
    llm_assistant_timeout = fields.Integer(
        string="AI Server Timeout", config_parameter='llm_assistant.timeout', default=DEFAULT_TIMEOUT,
        help="Seconds to wait for each reply of the AI server.",
    )
    llm_assistant_max_steps = fields.Integer(
        string="Maximum Steps", config_parameter='llm_assistant.max_steps', default=DEFAULT_MAX_STEPS,
        help="How many rounds of tool calls the assistant can make for one answer.",
    )
    llm_assistant_instructions = fields.Char(
        string="Extra Instructions", config_parameter='llm_assistant.instructions',
        help="Added to the instructions the assistant receives, e.g. house rules or vocabulary.",
    )
    llm_assistant_blocked_models = fields.Char(
        string="Blocked Models", config_parameter='llm_assistant.blocked_models',
        help="Comma-separated technical model names the assistant can never read or change. "
             "Wildcards are allowed, e.g. hr.employee*, account.move*.",
    )

    def action_llm_assistant_test_connection(self):
        """Check the server settings on this form, before saving them."""
        self.ensure_one()
        Client = self.env['llm.assistant.client']
        config = {
            **Client._get_config(),
            'base_url': self.llm_assistant_base_url or DEFAULT_BASE_URL,
            'api_key': self.llm_assistant_api_key,
            'timeout': min(self.llm_assistant_timeout or DEFAULT_TIMEOUT, 30),
        }
        try:
            model_names = Client._list_models(config)
        except LlmAssistantConnectionError as error:
            return self._llm_assistant_notification('danger', error.args[0])
        if self.llm_assistant_model and self.llm_assistant_model not in model_names:
            return self._llm_assistant_notification('warning', self.env._(
                "Connected, but the server has no model named %(model)s. Available models: %(models)s",
                model=self.llm_assistant_model, models=model_names,
            ))
        return self._llm_assistant_notification('success', self.env._(
            "Connected. Available models: %s", model_names,
        ))

    def _llm_assistant_notification(self, notification_type, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'type': notification_type, 'message': message, 'sticky': notification_type != 'success'},
        }
