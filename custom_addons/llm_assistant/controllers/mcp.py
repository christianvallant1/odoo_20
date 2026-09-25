import json

from odoo import http
from odoo.http import request
from odoo.tools.json import json_default

# Newest first; the server answers with the client's version when it knows it.
PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
SERVER_VERSION = '1.0.0'
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class LlmAssistantMcp(http.Controller):
    """A Model Context Protocol server (Streamable HTTP transport, JSON
    responses only) exposing the assistant tools to external AI clients.

    Clients authenticate with an API key of scope 'AI Assistant (MCP)'; tools
    run with the rights of the key's user, and write tools only create
    proposals that the user confirms in Odoo.
    """

    @http.route(
        '/llm_assistant/mcp', type='http', auth='bearer', bearer_scope='llm_assistant_mcp',
        methods=['POST'], csrf=False, save_session=False,
    )
    def mcp(self):
        # CSRF protection does not apply: only requests carrying an API key are
        # served, and browsers never add one on their own
        if not request.httprequest.headers.get('Authorization'):
            return request.make_json_response({'error': "An API key is required."}, status=401)
        if not request.env.user._is_internal():
            return request.make_json_response({'error': "Only internal users can use this server."}, status=403)
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True))
        except ValueError:
            return request.make_json_response(self._error(None, PARSE_ERROR, "Parse error"), status=400)

        if isinstance(payload, list):  # a batch, allowed by protocol versions before 2025-06-18
            if not payload:
                return request.make_json_response(self._error(None, INVALID_REQUEST, "Empty batch"), status=400)
            responses = [response for message in payload if (response := self._handle_message(message))]
        else:
            responses = self._handle_message(payload)
        if not responses:
            # only notifications or responses were received
            return request.make_response('', status=202)
        return request.make_json_response(responses)

    def _handle_message(self, message):
        """Answer one JSON-RPC message; return None when no answer is due."""
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            return self._error(None, INVALID_REQUEST, "Invalid Request")
        if 'method' not in message or 'id' not in message:
            # a notification, or a response to a request this server never sends
            return None
        message_id = message['id']
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return self._error(message_id, INVALID_PARAMS, "Params must be an object")
        match message['method']:
            case 'initialize':
                result = self._initialize(params)
            case 'ping':
                result = {}
            case 'tools/list':
                result = {'tools': request.env['llm.assistant.tools']._get_mcp_tools()}
            case 'tools/call':
                tool_names = {tool['name'] for tool in request.env['llm.assistant.tools']._get_tool_definitions()}
                if params.get('name') not in tool_names:
                    return self._error(message_id, INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
                result = self._call_tool(params['name'], params.get('arguments') or {})
            case method:
                return self._error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        return {'jsonrpc': '2.0', 'id': message_id, 'result': result}

    def _initialize(self, params):
        requested = params.get('protocolVersion')
        return {
            'protocolVersion': requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'odoo-llm-assistant', 'title': 'Odoo', 'version': SERVER_VERSION},
            'instructions': request.env['llm.assistant.tools']._get_instructions(),
        }

    def _call_tool(self, name, arguments):
        Tools = request.env['llm.assistant.tools'].with_context(llm_assistant_source='mcp')
        result = Tools._execute_tool(name, arguments)
        return {
            'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, default=json_default)}],
            # failed tool calls return only an error message
            'isError': list(result) == ['error'],
        }

    def _error(self, message_id, code, message):
        return {'jsonrpc': '2.0', 'id': message_id, 'error': {'code': code, 'message': message}}
