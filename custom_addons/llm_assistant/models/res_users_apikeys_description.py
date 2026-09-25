from odoo import fields, models


class ResUsersApikeysDescription(models.TransientModel):
    _inherit = 'res.users.apikeys.description'

    # A key with this scope only opens the MCP endpoint, where changes need
    # a confirmation in Odoo; it cannot be used for the external API.
    scope = fields.Selection(
        selection_add=[('llm_assistant_mcp', 'AI Assistant (MCP)')],
        ondelete={'llm_assistant_mcp': 'set default'},
    )
