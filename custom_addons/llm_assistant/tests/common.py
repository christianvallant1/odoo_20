from odoo.tests import TransactionCase, new_test_user


class LlmAssistantCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login='llm_user', name='Ann User', groups='base.group_user,base.group_partner_manager',
        )
        cls.other_user = new_test_user(
            cls.env, login='llm_other', name='Bob Other', groups='base.group_user,base.group_partner_manager',
        )
        cls.reader = new_test_user(cls.env, login='llm_reader', name='Cid Reader', groups='base.group_user')
        cls.partner = cls.env['res.partner'].create({
            'name': 'Azure Interior',
            'email': 'azure@example.com',
            'phone': '+1 555 0100',
            'is_company': True,
        })
        cls.contact = cls.env['res.partner'].create({'name': 'Brandon Freeman', 'parent_id': cls.partner.id})
        cls.Tools = cls.env['llm.assistant.tools'].with_user(cls.user)

    def execute(self, name, **arguments):
        return self.Tools._execute_tool(name, arguments)
