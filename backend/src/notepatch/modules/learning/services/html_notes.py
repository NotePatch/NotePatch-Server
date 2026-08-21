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
    "figure",
    "figcaption",
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
    "np-layout-grid",
    "np-layout-two-column",
    "np-stack",
    "np-section-card",
    "np-keyword",
    "np-divider",
    "np-code",
    "np-annotation",
    "np-annotation-marker",
    "np-source-block",
    "np-source-fragment",
}
KNOWLEDGE_POINT_ATTRIBUTE = re.compile(r'data-knowledge-point-id="([^"]+)"')


def sanitize_note_html(value: str) -> str:
    cleaned = nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "svg", "math"},
        attributes={
            "*": {"id", "data-knowledge-point-id", "data-note-asset-id", "data-language"},
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


def validate_note_structure(value: str) -> None:
    from html.parser import HTMLParser

    class StructureParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.root = False
            self.title = False
            self.summary = False
            self.sections: set[str] = set()

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            classes = set((values.get("class") or "").split())
            if "np-note" in classes and tag in {"article", "section", "div"}:
                self.root = True
            if "np-note-title" in classes and tag in {"h1", "h2"}:
                self.title = True
            if "np-note-summary" in classes:
                self.summary = True
            point_id = values.get("data-knowledge-point-id")
            if point_id and ({"np-note-section", "np-knowledge-point"} & classes):
                self.sections.add(point_id)

    parser = StructureParser()
    parser.feed(value)
    missing = []
    if not parser.root:
        missing.append(".np-note root")
    if not parser.title:
        missing.append(".np-note-title")
    if not parser.summary:
        missing.append(".np-note-summary")
    if not parser.sections:
        missing.append("knowledge-point sections")
    if missing:
        raise ValueError(f"Study note HTML is missing required structure: {', '.join(missing)}")
