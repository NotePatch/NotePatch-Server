from __future__ import annotations

import re

import nh3


ALLOWED_TAGS = {
    "article",
    "section",
    "header",
    "div",
    "span",
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "strong",
    "em",
    "mark",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "br",
    "sup",
    "sub",
}
ALLOWED_CLASSES = {
    "np-note",
    "np-note-header",
    "np-note-title",
    "np-note-summary",
    "np-note-section",
    "np-knowledge-point",
    "np-callout",
    "np-callout--tip",
    "np-callout--warning",
    "np-formula",
    "np-table",
    "np-highlight",
    "np-highlight--red",
    "np-highlight--yellow",
    "np-reinforcement",
}
KNOWLEDGE_POINT_ATTRIBUTE = re.compile(r'data-knowledge-point-id="([^"]+)"')


def sanitize_note_html(value: str) -> str:
    cleaned = nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "svg", "math"},
        attributes={
            "*": {"id", "data-knowledge-point-id"},
            "th": {"colspan", "rowspan", "scope"},
            "td": {"colspan", "rowspan"},
        },
        allowed_classes={tag: ALLOWED_CLASSES for tag in ALLOWED_TAGS},
        url_schemes=set(),
        link_rel=None,
    ).strip()
    if not cleaned:
        raise ValueError("Study note HTML is empty after sanitization")
    return cleaned


def referenced_knowledge_point_ids(value: str) -> set[str]:
    return set(KNOWLEDGE_POINT_ATTRIBUTE.findall(value))


def validate_knowledge_point_references(value: str, allowed_ids: set[str]) -> None:
    unknown = referenced_knowledge_point_ids(value) - allowed_ids
    if unknown:
        raise ValueError(f"Study note HTML contains unknown knowledge point ids: {', '.join(sorted(unknown))}")
