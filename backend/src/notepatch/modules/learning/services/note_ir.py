from __future__ import annotations

from collections import Counter
from html import escape
import re

from notepatch.modules.learning.schemas.skills import ScholarNotesResult
from notepatch.modules.learning.services.html_notes import sanitize_note_html

_CONTENT_CORRECTIONS = {
    "verbatim": {"ocr"},
    "spelling": {"ocr", "spelling"},
    "conceptual": {"ocr", "spelling", "concept"},
    "rewrite": {"ocr", "spelling", "concept"},
}


def validate_note_ir(
    result: ScholarNotesResult,
    source_blocks: list[dict],
    *,
    content_edit_level: str,
    layout_edit_level: str,
) -> None:
    source_by_id = {str(item["id"]): item for item in source_blocks}
    if not source_by_id:
        raise ValueError("Study note source blocks are empty")
    correction_by_source = {}
    allowed = _CONTENT_CORRECTIONS[content_edit_level]
    for correction in result.corrections:
        if correction.source_block_id not in source_by_id:
            raise ValueError(f"Correction references unknown source block: {correction.source_block_id}")
        if correction.correction_type not in allowed:
            raise ValueError(f"{content_edit_level} mode does not allow {correction.correction_type} corrections")
        if correction.correction_type == "concept":
            if correction.confidence < 0.9 or not correction.source_refs:
                raise ValueError("Concept corrections require confidence >= 0.9 and supporting sources")
        correction_by_source.setdefault(correction.source_block_id, []).append(correction)

    seen: list[str] = []
    positions = {block_id: index for index, block_id in enumerate(source_by_id)}
    order: list[int] = []
    known_points = {item.id for item in result.knowledge_points}
    block_ids = {item.id for item in result.note_ir.blocks}
    for block in result.note_ir.blocks:
        if block.knowledge_point_id not in known_points:
            raise ValueError(f"Note block references unknown knowledge point: {block.knowledge_point_id}")
        for relation in block.relations:
            if relation.target_block_id and relation.target_block_id not in block_ids:
                raise ValueError(f"Note relation targets unknown block: {relation.target_block_id}")
        source_document_ids = set()
        for source_id in block.source_block_ids:
            if source_id not in source_by_id:
                raise ValueError(f"Note IR references unknown source block: {source_id}")
            source_document_ids.add(str(source_by_id[source_id].get("document_id") or ""))
            seen.append(source_id)
            order.append(positions[source_id])
        if source_document_ids != {block.source_document_id}:
            raise ValueError(f"Note block {block.id} has an invalid source document relation")

        if content_edit_level != "rewrite":
            source_text = "\n".join(str(source_by_id[item].get("text") or "") for item in block.source_block_ids)
            output_text = block.text or block.latex or ""
            changed = (
                source_text != output_text
                if block.type == "code"
                else _normalized(source_text) != _normalized(output_text)
            )
            if changed and not all(item in correction_by_source for item in block.source_block_ids):
                raise ValueError(f"Changed note block {block.id} is missing an allowed correction record")

    if content_edit_level != "rewrite":
        counts = Counter(seen)
        missing = set(source_by_id) - set(counts)
        duplicated = sorted(item for item, count in counts.items() if count != 1)
        if missing or duplicated:
            raise ValueError(
                f"Note IR source coverage is invalid; missing={sorted(missing)}, duplicated={duplicated}"
            )
    if layout_edit_level in {"preserve", "minor"} and order != sorted(order):
        raise ValueError(f"{layout_edit_level} layout mode does not allow source block reordering")


def render_note_ir(result: ScholarNotesResult) -> str:
    point_map = {item.id: item for item in result.knowledge_points}
    grouped: dict[str, list] = {}
    for block in result.note_ir.blocks:
        grouped.setdefault(block.knowledge_point_id, []).append(block)
    sections = []
    for point_id, blocks in grouped.items():
        point = point_map[point_id]
        body = "".join(_render_block(block) for block in blocks)
        sections.append(
            f'<section id="{escape(point.section_id)}" class="np-note-section np-knowledge-point" '
            f'data-knowledge-point-id="{escape(point_id)}"><h2>{escape(point.name)}</h2>{body}</section>'
        )
    html = (
        '<article class="np-note"><header class="np-note-header">'
        f'<h1 class="np-note-title">{escape(result.title)}</h1>'
        f'<p class="np-note-summary">{escape(result.note_ir.summary)}</p></header>'
        + "".join(sections)
        + "</article>"
    )
    return sanitize_note_html(html)


def _render_block(block) -> str:
    block_id = escape(block.id)
    if block.preserve_as_image:
        main = (
            f'<figure class="np-source-fragment" data-note-asset-id="{block_id}">'
            '<figcaption>原稿中的图示或批注区域</figcaption></figure>'
        )
    elif block.type == "code":
        language = escape(block.language or "text")
        main = f'<pre class="np-code"><code data-language="{language}">{escape(block.text)}</code></pre>'
    elif block.type == "formula":
        main = f'<div class="np-formula">{escape(block.latex or block.text)}</div>'
    elif block.type == "table" and block.table_html:
        main = sanitize_note_html(block.table_html)
    elif block.type in {"annotation", "diagram"}:
        main = f'<aside class="np-annotation">{escape(block.text)}</aside>'
    else:
        main = f'<p>{escape(block.text)}</p>'
    if block.relations:
        items = []
        for relation in block.relations:
            label = relation.text or relation.type
            target = f" → {relation.target_block_id}" if relation.target_block_id else ""
            items.append(f'<li><span class="np-annotation-marker">{escape(label + target)}</span></li>')
        main += '<ul class="np-annotation">' + "".join(items) + "</ul>"
    return f'<div id="{block_id}" class="np-source-block">{main}</div>'


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
