from odoo.tests import BaseCase, tagged

from odoo.addons.llm_assistant.tools import markdown_to_html


@tagged('post_install', '-at_install')
class TestMarkdown(BaseCase):

    def test_blocks(self):
        self.assertEqual(
            markdown_to_html("# Contacts\nTwo found:\n\n- **Azure** Interior\n- *Deco* Addict\n\n1. first\n2. second\n\n---"),
            '<p><strong>Contacts</strong></p><p>Two found:</p>'
            '<ul><li><strong>Azure</strong> Interior</li><li><em>Deco</em> Addict</li></ul>'
            '<ol><li>first</li><li>second</li></ol><hr/>',
        )

    def test_table_and_code(self):
        self.assertEqual(
            markdown_to_html("| Name | Phone |\n|---|:---:|\n| Azure | +1 555 |\n\n```\na < b\n```\nUse `sale.order`."),
            '<table class="table table-sm table-bordered"><thead><tr><th>Name</th><th>Phone</th></tr></thead>'
            '<tbody><tr><td>Azure</td><td>+1 555</td></tr></tbody></table>'
            '<pre><code>a &lt; b</code></pre><p>Use <code>sale.order</code>.</p>',
        )

    def test_links_and_escaping(self):
        self.assertEqual(
            markdown_to_html("[Order](https://example.com/o?a=1&b=2) and [x](javascript:alert(1))"),
            '<p><a href="https://example.com/o?a=1&amp;b=2">Order</a> and [x](javascript:alert(1))</p>',
        )
        self.assertEqual(
            markdown_to_html('<script>alert("x")</script> **<b>bold</b>** `<i>`'),
            '<p>&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; <strong>&lt;b&gt;bold&lt;/b&gt;</strong> <code>&lt;i&gt;</code></p>',
        )
        self.assertEqual(markdown_to_html("sale_order_line and 2 * 3 * 4"), '<p>sale_order_line and 2 * 3 * 4</p>')
