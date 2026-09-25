"""Render the Markdown that LLMs write in their answers as chat HTML.

Only a small subset is supported: paragraphs, line breaks, headings (shown
bold), bullet and numbered lists, tables, fenced code blocks, and inline
bold, italic, code and links. The text is escaped before any tag is added,
so whatever HTML the LLM writes is shown as text.
"""
import re

from markupsafe import Markup, escape

FENCE_RE = re.compile(r'^\s*```')
HEADING_RE = re.compile(r'^\s*#{1,6}\s+(.*)$')
RULE_RE = re.compile(r'^\s*([-*_])(\s*\1){2,}\s*$')
BULLET_RE = re.compile(r'^\s*[-*+]\s+(.*)$')
NUMBERED_RE = re.compile(r'^\s*\d+[.)]\s+(.*)$')
TABLE_RE = re.compile(r'^\s*\|(.*)\|\s*$')
TABLE_SEPARATOR_RE = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$')
CODE_SPAN_RE = re.compile(r'`([^`]+)`')
BOLD_RE = re.compile(r'\*\*(.+?)\*\*|__(.+?)__')
ITALIC_RE = re.compile(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])')
# applied to escaped text: the URL can only be absolute http(s) or site-relative
LINK_RE = re.compile(r'\[([^\]]+)\]\(((?:https?://|/)[^\s)]*)\)')


def markdown_to_html(text):
    """Return ``text`` (Markdown) as ``Markup``."""
    blocks = []
    paragraph = []
    lines = (text or '').replace('\r\n', '\n').split('\n')
    index = 0

    def flush_paragraph():
        if paragraph:
            blocks.append(Markup('<p>%s</p>') % Markup('<br/>').join(paragraph))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if FENCE_RE.match(line):
            flush_paragraph()
            code = []
            index += 1
            while index < len(lines) and not FENCE_RE.match(lines[index]):
                code.append(lines[index])
                index += 1
            blocks.append(Markup('<pre><code>%s</code></pre>') % '\n'.join(code))
        elif match := HEADING_RE.match(line):
            flush_paragraph()
            blocks.append(Markup('<p><strong>%s</strong></p>') % _inline(match[1]))
        elif RULE_RE.match(line):
            flush_paragraph()
            blocks.append(Markup('<hr/>'))
        elif BULLET_RE.match(line) or NUMBERED_RE.match(line):
            flush_paragraph()
            regex = BULLET_RE if BULLET_RE.match(line) else NUMBERED_RE
            items = []
            while index < len(lines) and (match := regex.match(lines[index])):
                items.append(Markup('<li>%s</li>') % _inline(match[1]))
                index += 1
            tag = Markup('ul') if regex is BULLET_RE else Markup('ol')
            blocks.append(Markup('<%s>%s</%s>') % (tag, Markup().join(items), tag))
            continue
        elif TABLE_RE.match(line):
            flush_paragraph()
            rows = []
            while index < len(lines) and TABLE_RE.match(lines[index]):
                if not TABLE_SEPARATOR_RE.match(lines[index]):
                    rows.append([cell.strip() for cell in TABLE_RE.match(lines[index])[1].split('|')])
                index += 1
            blocks.append(_table(rows))
            continue
        elif line.strip():
            paragraph.append(_inline(line.strip()))
        else:
            flush_paragraph()
        index += 1
    flush_paragraph()
    return Markup().join(blocks)


def _table(rows):
    if not rows:
        return Markup()
    header, *body = rows
    head = Markup().join(Markup('<th>%s</th>') % _inline(cell) for cell in header)
    lines = Markup().join(
        Markup('<tr>%s</tr>') % Markup().join(Markup('<td>%s</td>') % _inline(cell) for cell in row)
        for row in body
    )
    return Markup(
        '<table class="table table-sm table-bordered"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>',
    ) % (head, lines)


def _inline(text):
    """Escape ``text`` and render its inline Markdown."""
    parts = []
    # code spans are kept verbatim: split them out before the other rules
    for position, part in enumerate(CODE_SPAN_RE.split(text)):
        if position % 2:
            parts.append(Markup('<code>%s</code>') % part)
            continue
        html = str(escape(part))
        html = LINK_RE.sub(r'<a href="\2">\1</a>', html)
        html = BOLD_RE.sub(lambda match: f'<strong>{match[1] or match[2]}</strong>', html)
        html = ITALIC_RE.sub(r'<em>\1</em>', html)
        parts.append(Markup(html))  # escaped above; only the tags above were added
    return Markup().join(parts)
