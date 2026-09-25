import requests

from odoo import api, models
from odoo.exceptions import UserError

DEFAULT_BASE_URL = 'http://localhost:11434/v1'
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_STEPS = 8
MAX_ERROR_LENGTH = 500


class LlmAssistantConnectionError(UserError):
    """The LLM server could not be reached or gave an unusable answer."""


class LlmAssistantClient(models.AbstractModel):
    """Client for LLM servers with an OpenAI-compatible chat completions API:
    Ollama, vLLM, llama.cpp, LM Studio and most hosted providers."""
    _name = 'llm.assistant.client'
    _description = "AI Assistant LLM Client"

    @api.model
    def _get_config(self):
        # sudo: the API key is readable by administrators only; it is sent to
        # the configured server and never returned to users
        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        return {
            'base_url': IrConfigParameter.get_str('llm_assistant.base_url') or DEFAULT_BASE_URL,
            'model': IrConfigParameter.get_str('llm_assistant.model'),
            'api_key': IrConfigParameter.get_str('llm_assistant.api_key'),
            'timeout': IrConfigParameter.get_int('llm_assistant.timeout') or DEFAULT_TIMEOUT,
            'max_steps': IrConfigParameter.get_int('llm_assistant.max_steps') or DEFAULT_MAX_STEPS,
            'instructions': IrConfigParameter.get_str('llm_assistant.instructions'),
        }

    @api.model
    def _chat_completion(self, messages, tools=None, config=None):
        """Send the conversation to the LLM and return its reply message, a
        dict with ``content`` and possibly ``tool_calls``."""
        config = config or self._get_config()
        if not config['model']:
            raise LlmAssistantConnectionError(self.env._(
                "No AI model is configured. An administrator can set one in Settings, AI Assistant.",
            ))
        payload = {'model': config['model'], 'messages': messages}
        if tools:
            payload['tools'] = tools
        data = self._request('POST', '/chat/completions', config, json=payload)
        try:
            message = data['choices'][0]['message']
        except (KeyError, IndexError, TypeError):
            message = None
        if not isinstance(message, dict):
            raise LlmAssistantConnectionError(self.env._(
                "The AI server gave an unexpected answer: %s", str(data)[:MAX_ERROR_LENGTH],
            ))
        return message

    @api.model
    def _list_models(self, config=None):
        """Return the names of the models the server offers."""
        data = self._request('GET', '/models', config or self._get_config())
        try:
            return sorted(model['id'] for model in data['data'])
        except (KeyError, TypeError):
            raise LlmAssistantConnectionError(self.env._(
                "The AI server gave an unexpected answer: %s", str(data)[:MAX_ERROR_LENGTH],
            )) from None

    @api.model
    def _request(self, method, path, config, **kwargs):
        url = config['base_url'].rstrip('/') + path
        headers = {'Authorization': f"Bearer {config['api_key']}"} if config.get('api_key') else {}
        try:
            response = requests.request(method, url, headers=headers, timeout=config['timeout'], **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as error:
            raise LlmAssistantConnectionError(self.env._(
                "The AI server at %(url)s answered with an error: %(error)s",
                url=url, error=f"{error} {error.response.text[:MAX_ERROR_LENGTH]}",
            )) from error
        except (requests.RequestException, ValueError) as error:
            raise LlmAssistantConnectionError(self.env._(
                "Could not reach the AI server at %(url)s: %(error)s", url=url, error=str(error),
            )) from error
