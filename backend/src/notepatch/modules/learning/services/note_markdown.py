from __future__ import annotations

from html import escape

from markdown_it import MarkdownIt

from notepatch.modules.learning.services.html_notes import sanitize_note_html


NOTE_MARKDOWN_RENDERER_REVISION = "note-ir-markdown-v1"


def _markdown_parser() -> MarkdownIt:
    parser = MarkdownIt(
        "commonmark",
        {
            "breaks": True,
            "html": False,
            "linkify": False,
            "typographer": False,
        },
    )
    parser.enable("table")
    parser.disable(["autolink", "html_block", "html_inline", "image", "link"])

    def render_heading(tokens, index, options, env):
        token = tokens[index]
        original_tag = token.tag
        source_level = int(original_tag[1:])
        token.tag = "h3" if source_level <= 2 else "h4"
        try:
            return parser.renderer.renderToken(tokens, index, options, env)
        finally:
            token.tag = original_tag

    def render_fence(tokens, index, _options, _env):
        token = tokens[index]
        language = (token.info or "text").strip().split(maxsplit=1)[0] or "text"
        return (
            f'<pre class="np-code"><code data-language="{escape(language)}">'
            f"{escape(token.content)}</code></pre>\n"
        )

    parser.renderer.rules["heading_open"] = render_heading
    parser.renderer.rules["heading_close"] = render_heading
    parser.renderer.rules["fence"] = render_fence
    return parser


_MARKDOWN = _markdown_parser()


def render_note_markdown(value: str) -> str:
    """Render a Note IR Markdown fragment without links, images, or raw HTML."""
    source = value.strip()
    if not source:
        return "<p></p>"
    return sanitize_note_html(_MARKDOWN.render(source))
