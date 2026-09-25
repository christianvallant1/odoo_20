{
    'name': 'AI Assistant',
    'version': '1.0.0',
    'category': 'Productivity',
    'summary': 'Chat with a self-hosted LLM that looks up Odoo data and proposes changes you confirm',
    'depends': ['base_setup', 'mail'],
    'data': [
        'security/ir.access.csv',
        'data/llm_assistant_data.xml',
        'views/llm_assistant_proposal_views.xml',
        'views/llm_assistant_job_views.xml',
        'views/res_config_settings_views.xml',
        'views/llm_assistant_menus.xml',
    ],
    'application': True,
    'author': 'christianvallant1',
    'license': 'LGPL-3',
}
