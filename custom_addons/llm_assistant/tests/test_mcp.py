import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

MCP_URL = '/llm_assistant/mcp'


@tagged('post_install', '-at_install')
class TestLlmAssistantMcp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='mcp_user', groups='base.group_user,base.group_partner_manager')
        cls.partner = cls.env['res.partner'].create({'name': 'Azure Interior', 'phone': '+1 555 0100'})
        ApiKeys = cls.env['res.users.apikeys'].with_user(cls.user)
        expiration = fields.Datetime.now() + timedelta(hours=12)
        cls.key = ApiKeys._generate('llm_assistant_mcp', 'MCP client', expiration)
        cls.rpc_key = ApiKeys._generate('rpc', 'Script', expiration)

    def mcp(self, payload, key=None):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if key is not False:
            headers['Authorization'] = f'Bearer {key or self.key}'
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return self.url_open(MCP_URL, data=data, headers=headers)

    def call_tool(self, name, **arguments):
        response = self.mcp({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
        self.assertEqual(response.status_code, 200)
        result = response.json()['result']
        return result['isError'], json.loads(result['content'][0]['text'])

    def test_handshake(self):
        response = self.mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1.0'},
        }})
        self.assertEqual(response.status_code, 200)
        result = response.json()['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertIn('tools', result['capabilities'])
        self.assertIn('propose_create', result['instructions'])

        unknown_version = self.mcp({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '1999-01-01'}})
        self.assertEqual(unknown_version.json()['result']['protocolVersion'], '2025-11-25')

        self.assertEqual(self.mcp({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code, 202)
        self.assertEqual(self.mcp({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}).json(), {'jsonrpc': '2.0', 'id': 3, 'result': {}})

        tools = self.mcp({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'}).json()['result']['tools']
        by_name = {tool['name']: tool for tool in tools}
        self.assertTrue(by_name['search_records']['annotations']['readOnlyHint'])
        self.assertFalse(by_name['propose_update']['annotations']['readOnlyHint'])
        self.assertEqual(by_name['search_records']['inputSchema']['required'], ['model'])

    def test_tools(self):
        is_error, result = self.call_tool('search_records', model='res.partner', domain=[['id', '=', self.partner.id]], fields=['phone'])
        self.assertFalse(is_error)
        self.assertEqual(result['records'], [{'id': self.partner.id, 'phone': '+1 555 0100'}])

        is_error, result = self.call_tool('propose_update', model='res.partner', ids=[self.partner.id], values={'phone': '+1 555 0199'})
        self.assertFalse(is_error)
        self.assertEqual(result['status'], 'pending')
        self.assertIn(f"llm_assistant_proposal_action/{result['proposal_id']}", result['url'])
        proposal = self.env['llm.assistant.proposal'].browse(result['proposal_id'])
        self.assertRecordValues(proposal, [{'user_id': self.user.id, 'source': 'mcp', 'state': 'pending'}])
        self.assertEqual(self.partner.phone, '+1 555 0100', "nothing changes before the confirmation")

        is_error, result = self.call_tool('search_records', model='ir.config_parameter')
        self.assertTrue(is_error)
        self.assertIn('not allowed', result['error'])

    def test_batch(self):
        response = self.mcp([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
        ])
        self.assertEqual([answer['id'] for answer in response.json()], [1, 2])

    def test_protocol_errors(self):
        for payload, code in [
            ('{not json', -32700),
            ({'id': 1, 'method': 'ping'}, -32600),
            ({'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}, -32601),
            ({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'no_such_tool'}}, -32602),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.mcp(payload).json()['error']['code'], code)

    @mute_logger('odoo.http')
    def test_authentication(self):
        ping = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        self.assertEqual(self.mcp(ping, key=False).status_code, 401)
        self.assertEqual(self.mcp(ping, key='not-a-key').status_code, 401)
        self.assertEqual(self.mcp(ping, key=self.rpc_key).status_code, 401, "an API key for the external API is refused")
        self.assertEqual(self.url_open(MCP_URL, headers={'Authorization': f'Bearer {self.key}'}).status_code, 405)
