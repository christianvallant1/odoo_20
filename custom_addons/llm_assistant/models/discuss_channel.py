from odoo import api, models
from odoo.exceptions import AccessError


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def _message_post_after_hook(self, message):
        self._llm_assistant_handle_message(message)
        return super()._message_post_after_hook(message)

    @api.model
    def action_llm_assistant_open_chat(self):
        """Open the current user's chat with the assistant in Discuss."""
        if not self.env.user._is_internal():
            raise AccessError(self.env._("Only internal users can chat with the AI Assistant."))
        assistant = self.env.ref('llm_assistant.partner_assistant')
        channel = self._get_or_create_chat([assistant.id])
        return {
            'type': 'ir.actions.act_url',
            'url': f'/odoo/action-mail.action_discuss?active_id={channel.id}',
            'target': 'self',
        }

    def _llm_assistant_handle_message(self, message):
        """Queue an answer when an internal user writes to the assistant."""
        if self.channel_type != 'chat' or message.message_type != 'comment':
            return
        assistant = self.env.ref('llm_assistant.partner_assistant', raise_if_not_found=False)
        if not assistant or message.author_id == assistant or assistant not in self.channel_member_ids.partner_id:
            return
        user = self.env.user
        if message.author_id != user.partner_id or not user._is_internal():
            return
        # sudo: users cannot create jobs themselves; the job answers as the
        # assistant but runs its tools with this user's rights
        self.env['llm.assistant.job'].sudo()._enqueue(self, user, message)

    def _llm_assistant_post(self, body):
        """Post ``body`` (HTML) in this chat as the assistant."""
        self.ensure_one()
        assistant = self.env.ref('llm_assistant.partner_assistant')
        self._llm_assistant_notify_typing(False)
        # sudo: the assistant is not a user, it posts like OdooBot does
        return self.sudo().message_post(
            author_id=assistant.id,
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
        )

    def _llm_assistant_notify_typing(self, is_typing):
        self.ensure_one()
        assistant = self.env.ref('llm_assistant.partner_assistant')
        # sudo: members are read to find the assistant's, and only it types
        member = self.sudo().channel_member_ids.filtered(lambda member: member.partner_id == assistant)
        member._notify_typing(is_typing)
