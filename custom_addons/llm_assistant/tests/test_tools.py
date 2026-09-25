from odoo.tests import tagged

from .common import LlmAssistantCommon


@tagged('post_install', '-at_install')
class TestLlmAssistantTools(LlmAssistantCommon):

    def test_search_records(self):
        result = self.execute(
            'search_records', model='res.partner', domain=[['name', 'ilike', 'azure'], ['id', 'child_of', self.partner.id]],
            fields=['name', 'email'],
        )
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['records'], [{'id': self.partner.id, 'name': 'Azure Interior', 'email': 'azure@example.com'}])

    def test_search_records_defaults(self):
        # some LLMs send the domain as a JSON string
        result = self.execute('search_records', model='res.partner', domain='[["parent_id", "=", %d]]' % self.partner.id)
        self.assertEqual(result['total'], 1)
        record = result['records'][0]
        self.assertEqual(record['display_name'], 'Azure Interior, Brandon Freeman')
        self.assertNotIn('image_1920', record, "binary fields are never read")

    def test_search_records_limit(self):
        result = self.execute('search_records', model='res.partner', domain=[['id', 'child_of', self.partner.id]], limit=1)
        self.assertEqual(result['total'], 2, "the total counts all matches")
        self.assertEqual(len(result['records']), 1)

    def test_count_and_group_records(self):
        domain = [['id', 'child_of', self.partner.id]]
        self.assertEqual(self.execute('count_records', model='res.partner', domain=domain), {'model': 'res.partner', 'count': 2})
        result = self.execute('group_records', model='res.partner', domain=domain, groupby=['parent_id'], order='parent_id')
        self.assertEqual(result['groups'], [
            {'parent_id': [self.partner.id, 'Azure Interior'], '__count': 1},
            {'parent_id': False, '__count': 1},
        ])

    def test_list_models_and_get_fields(self):
        found = self.execute('list_models', query='contact')['models']
        self.assertIn('res.partner', [model['model'] for model in found])
        fields = self.execute('get_fields', model='res.partner', query='email')['fields']
        self.assertIn('email', [field['name'] for field in fields])
        user_fields = self.execute('get_fields', model='res.users')['fields']
        self.assertFalse([field for field in user_fields if 'password' in field['name']])

    def test_errors_are_returned_to_the_llm(self):
        for name, arguments, message in [
            ('search_records', {'model': 'no.such.model'}, "Unknown model"),
            ('search_records', {'model': 'res.partner', 'domain': [['name', 'bad', 1]]}, "bad"),
            ('search_records', {'model': 'res.partner', 'fields': ['no_such_field']}, "no_such_field"),
            ('search_records', {'model': 'res.partner', 'unexpected': 1}, "unexpected"),
            ('search_records', {'model': 'res.partner', 'order': 'name; drop table res_partner'}, "order"),
            ('no_such_tool', {}, "Unknown tool"),
        ]:
            with self.subTest(name=name, arguments=arguments):
                result = self.Tools._execute_tool(name, arguments)
                self.assertEqual(list(result), ['error'])
                self.assertIn(message, result['error'])

    def test_secrets_are_off_limits(self):
        Tools = self.env['llm.assistant.tools']  # even for the superuser
        self.assertIn('not allowed', Tools._execute_tool('search_records', {'model': 'ir.config_parameter'})['error'])
        self.assertIn('not allowed', Tools._execute_tool('search_records', {'model': 'res.users.apikeys'})['error'])
        result = Tools._execute_tool('search_records', {'model': 'res.users', 'domain': [['password', '=', 'admin']]})
        self.assertIn('cannot be used', result['error'])
        result = Tools._execute_tool('search_records', {'model': 'res.users', 'domain': [['partner_id.signup_token', '!=', False]]})
        self.assertIn('cannot be used', result['error'])

    def test_user_access_rights_apply(self):
        result = self.execute('search_records', model='ir.cron')
        self.assertIn('error', result)
        self.assertNotIn('ir.cron', [model['model'] for model in self.execute('list_models', query='cron')['models']])

    def test_blocked_models_setting(self):
        self.env['ir.config_parameter'].set_str('llm_assistant.blocked_models', 'res.partner*, crm.*')
        self.assertIn('not allowed', self.execute('search_records', model='res.partner')['error'])
        self.assertIn('not allowed', self.execute('get_fields', model='res.partner.category')['error'])
