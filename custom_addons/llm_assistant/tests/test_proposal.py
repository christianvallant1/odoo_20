from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import LlmAssistantCommon


@tagged('post_install', '-at_install')
class TestLlmAssistantProposal(LlmAssistantCommon):

    def propose(self, name, **arguments):
        result = self.execute(name, **arguments)
        self.assertNotIn('error', result)
        self.assertEqual(result['status'], 'pending')
        return self.env['llm.assistant.proposal'].browse(result['proposal_id'])

    def test_update_waits_for_confirmation(self):
        proposal = self.propose(
            'propose_update', model='res.partner', ids=[self.partner.id], values={'phone': '+1 555 0199'},
            summary="The customer called with a new number.",
        )
        self.assertEqual(self.partner.phone, '+1 555 0100', "nothing changes before the confirmation")
        self.assertRecordValues(proposal, [{
            'user_id': self.user.id,
            'operation': 'write',
            'res_model': 'res.partner',
            'res_ids': [self.partner.id],
            'values': {'phone': '+1 555 0199'},
            'description': "The customer called with a new number.",
            'source': 'chat',
        }])
        self.assertIn('Update 1 x Contact', proposal.name)
        self.assertIn('+1 555 0100', proposal.preview, "the preview shows the current value")
        self.assertIn('+1 555 0199', proposal.preview, "and the new one")

        proposal.with_user(self.user).action_confirm()
        self.assertEqual(proposal.state, 'done')
        self.assertTrue(proposal.handled_date)
        self.assertEqual(self.partner.phone, '+1 555 0199')
        with self.assertRaises(UserError):
            proposal.with_user(self.user).action_confirm()

    def test_only_the_requester_handles_a_proposal(self):
        proposal = self.propose('propose_delete', model='res.partner', ids=[self.contact.id])
        with self.assertRaises(AccessError):
            proposal.with_user(self.other_user).action_confirm()
        with self.assertRaises(AccessError):
            proposal.with_user(self.other_user).action_reject()
        self.assertFalse(self.env['llm.assistant.proposal'].with_user(self.other_user).search([('id', '=', proposal.id)]))
        self.assertTrue(self.contact.exists())

    def test_reject(self):
        proposal = self.propose('propose_delete', model='res.partner', ids=[self.contact.id])
        proposal.with_user(self.user).action_reject()
        self.assertEqual(proposal.state, 'rejected')
        self.assertTrue(self.contact.exists())

    def test_create(self):
        proposal = self.propose('propose_create', model='res.partner', values={'name': 'Jane Buyer', 'parent_id': self.partner.id})
        self.assertIn('Azure Interior', proposal.preview, "many2one values show the record name")
        proposal.with_user(self.user).action_confirm()
        created = self.env['res.partner'].browse(proposal.res_ids)
        self.assertRecordValues(created, [{'name': 'Jane Buyer', 'parent_id': self.partner.id}])
        action = proposal.with_user(self.user).action_open_records()
        self.assertEqual((action['res_model'], action['res_id']), ('res.partner', created.id))

    def test_create_with_lines(self):
        category = self.env['res.partner.category'].create({'name': 'VIP'})
        proposal = self.propose('propose_create', model='res.partner', values={
            'name': 'Jane Buyer',
            'category_id': [[4, category.id]],
            'child_ids': [[0, 0, {'name': 'Jane Assistant'}]],
        })
        self.assertIn('Add VIP', proposal.preview)
        self.assertIn('New: Name: Jane Assistant', proposal.preview)
        proposal.with_user(self.user).action_confirm()
        created = self.env['res.partner'].browse(proposal.res_ids)
        self.assertEqual(created.category_id, category)
        self.assertEqual(created.child_ids.name, 'Jane Assistant')

    def test_delete(self):
        proposal = self.propose('propose_delete', model='res.partner', ids=[self.contact.id])
        self.assertIn('Brandon Freeman', proposal.preview)
        proposal.with_user(self.user).action_confirm()
        self.assertEqual(proposal.state, 'done')
        self.assertFalse(self.contact.exists())

    def test_action(self):
        proposal = self.propose('propose_action', model='res.partner', ids=[self.contact.id], method='action_archive')
        self.assertTrue(self.contact.active)
        proposal.with_user(self.user).action_confirm()
        self.assertEqual(proposal.state, 'done')
        self.assertFalse(self.contact.active)

    def test_failure_is_recorded(self):
        proposal = self.propose('propose_update', model='res.partner', ids=[self.contact.id], values={'type': 'no_such_type'})
        proposal.with_user(self.user).action_confirm()
        self.assertEqual(proposal.state, 'failed')
        self.assertIn('no_such_type', proposal.error)
        self.assertEqual(self.contact.type, 'contact')

    def test_invalid_proposals_are_refused(self):
        for name, arguments, message in [
            ('propose_update', {'model': 'res.users', 'ids': [self.user.id], 'values': {'name': 'X'}}, "not allowed"),
            ('propose_update', {'model': 'ir.cron', 'ids': [1], 'values': {'active': False}}, "not allowed"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {'no_field': 1}}, "no_field"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {'display_name': 'X'}}, "cannot be set"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {}}, "JSON object"),
            ('propose_update', {'model': 'res.partner', 'ids': [0], 'values': {'name': 'X'}}, "No res.partner"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {'user_ids': [[1, self.user.id, {'name': 'X'}]]}}, "cannot change"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {'child_ids': [[0, 0, {'no_field': 1}]]}}, "no_field"),
            ('propose_update', {'model': 'res.partner', 'ids': [self.partner.id], 'values': {'child_ids': 'Jane'}}, "list of IDs"),
            ('propose_create', {'model': 'res.partner.category', 'values': {'color': 3}}, "Missing required fields"),
            ('propose_action', {'model': 'res.partner', 'ids': [self.partner.id], 'method': '_compute_display_name'}, "Private"),
            ('propose_action', {'model': 'res.partner', 'ids': [self.partner.id], 'method': 'write'}, "propose_update"),
            ('propose_action', {'model': 'res.partner', 'ids': [self.partner.id], 'method': 'no_such_method'}, "does not exist"),
        ]:
            with self.subTest(name=name, arguments=arguments):
                result = self.Tools._execute_tool(name, arguments)
                self.assertIn(message, result.get('error', ''))
        self.assertFalse(self.env['llm.assistant.proposal'].search([('user_id', '=', self.user.id)]))

    def test_proposals_need_the_user_rights(self):
        result = self.env['llm.assistant.tools'].with_user(self.reader)._execute_tool('propose_update', {
            'model': 'res.partner', 'ids': [self.partner.id], 'values': {'phone': '+1 555 0199'},
        })
        self.assertIn('error', result)
        self.assertFalse(self.env['llm.assistant.proposal'].search([('user_id', '=', self.reader.id)]))

    def test_get_proposal(self):
        proposal = self.propose('propose_delete', model='res.partner', ids=[self.contact.id])
        proposal.with_user(self.user).action_reject()
        result = self.execute('get_proposal', proposal_id=proposal.id)
        self.assertEqual((result['proposal_id'], result['status']), (proposal.id, 'rejected'))
        other_result = self.env['llm.assistant.tools'].with_user(self.other_user)._execute_tool('get_proposal', {'proposal_id': proposal.id})
        self.assertIn('not found', other_result['error'])
