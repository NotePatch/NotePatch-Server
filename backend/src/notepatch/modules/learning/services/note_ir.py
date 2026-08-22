from __future__ import annotations

from collections import Counter
from html import escape
import re

from notepatch.modules.learning.schemas.skills import NoteEvidenceRef, ScholarNotesResult
from notepatch.modules.learning.services.html_notes import sanitize_note_html
from notepatch.modules.learning.services.note_markdown import render_note_markdown

_CONTENT_CORRECTIONS = {
    "verbatim": {"ocr"},
    "spelling": {"ocr", "spelling"},
    "conceptual": {"ocr", "spelling", "concept"},
    "rewrite": {"ocr", "spelling", "concept"},
}

_BRANDING_SHORT_SUFFIX = re.compile(
    r"(学校|大学|学院|中学|小学|公司|集团|出版社|制造厂|文具厂|\b(?:school|university|college|academy|company|corporation|corp|inc|ltd|publisher)\.?)$",
    re.IGNORECASE,
)
_BRANDING_EXPLICIT = re.compile(
    r"((?:生产商|制造商|出品方)\s*[:：]|版权所有|\bmanufacturer\s*[:：]|\bmade\s+by\b|\bcopyright\b)",
    re.IGNORECASE,
)


def is_notebook_branding_text(value: str) -> bool:
    """Return true only for high-confidence, standalone notebook identity marks."""
    text = _normalized(value)
    if not text or len(text) > 80 or "\n" in value.strip():
        return False
    return bool(_BRANDING_EXPLICIT.search(text) or _BRANDING_SHORT_SUFFIX.search(text))


def validate_note_ir(
    result: ScholarNotesResult,
    source_blocks: list[dict],
    *,
    content_edit_level: str,
    layout_edit_level: str,
    completion_evidence: dict[str, dict] | None = None,
) -> None:
    source_by_id = {str(item["id"]): item for item in source_blocks}
    if not source_by_id:
        raise ValueError("Study note source blocks are empty")
    correction_by_source = {}
    allowed = _CONTENT_CORRECTIONS[content_edit_level]
    excluded_ids: set[str] = set()
    for exclusion in result.excluded_source_blocks:
        if content_edit_level == "verbatim":
            raise ValueError("verbatim mode does not allow notebook identity exclusions")
        if exclusion.source_block_id not in source_by_id:
            raise ValueError(f"Notebook identity exclusion references unknown source block: {exclusion.source_block_id}")
        if exclusion.source_block_id in excluded_ids:
            raise ValueError(f"Notebook identity source block is excluded more than once: {exclusion.source_block_id}")
        excluded_ids.add(exclusion.source_block_id)

    required_exclusions = {
        source_id
        for source_id, source in source_by_id.items()
        if content_edit_level != "verbatim"
        and (
            bool(source.get("notebook_identity_candidate"))
            or is_notebook_branding_text(str(source.get("text") or ""))
        )
    }
    missing_exclusions = required_exclusions - excluded_ids
    if missing_exclusions:
        raise ValueError(
            f"Editable note must exclude notebook identity blocks: {sorted(missing_exclusions)}"
        )

    for correction in result.corrections:
        if correction.source_block_id not in source_by_id:
            raise ValueError(f"Correction references unknown source block: {correction.source_block_id}")
        if correction.source_block_id in excluded_ids:
            raise ValueError(f"Excluded notebook identity block cannot have a correction: {correction.source_block_id}")
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
    evidence_by_id = completion_evidence or {}
    for block in result.note_ir.blocks:
        if block.knowledge_point_id not in known_points:
            raise ValueError(f"Note block references unknown knowledge point: {block.knowledge_point_id}")
        for relation in block.relations:
            if relation.target_block_id and relation.target_block_id not in block_ids:
                raise ValueError(f"Note relation targets unknown block: {relation.target_block_id}")
        if block.origin == "evidence_supplement":
            if content_edit_level != "rewrite":
                raise ValueError("Evidence supplements are only allowed in rewrite mode")
            evidence = []
            for source_ref in block.source_refs:
                item = evidence_by_id.get(source_ref.evidence_id)
                if item is None:
                    raise ValueError(
                        f"Evidence supplement references unknown evidence: {source_ref.evidence_id}"
                    )
                evidence.append(item)
            if not any(bool(item.get("authoritative")) for item in evidence):
                raise ValueError("Evidence supplements require at least one authoritative source")
            continue

        source_document_ids = set()
        for source_id in block.source_block_ids:
            if source_id not in source_by_id:
                raise ValueError(f"Note IR references unknown source block: {source_id}")
            if source_id in excluded_ids:
                raise ValueError(f"Excluded notebook identity block is still mapped: {source_id}")
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

    counts = Counter(seen)
    required_source_ids = set(source_by_id) - excluded_ids
    if content_edit_level != "rewrite":
        missing = required_source_ids - set(counts)
        duplicated = sorted(item for item, count in counts.items() if count != 1)
        if missing or duplicated:
            raise ValueError(
                f"Note IR source coverage is invalid; missing={sorted(missing)}, duplicated={duplicated}"
            )
    else:
        missing = required_source_ids - set(counts)
        duplicated = sorted(item for item, count in counts.items() if count != 1)
        if missing or duplicated:
            raise ValueError(
                f"Rewrite note source coverage is invalid; missing={sorted(missing)}, duplicated={duplicated}"
            )
        manuscript_blocks = [
            block for block in result.note_ir.blocks if block.origin == "manuscript"
        ]
        source_counts_by_document = Counter(
            str(source_by_id[source_id].get("document_id") or "")
            for source_id in required_source_ids
        )
        if (
            layout_edit_level == "reflow"
            and any(count >= 2 for count in source_counts_by_document.values())
            and not any(len(block.source_block_ids) >= 2 for block in manuscript_blocks)
        ):
            raise ValueError(
                "rewrite + reflow must consolidate related source blocks instead of preserving one OCR block per output block"
            )
    if excluded_ids:
        visible_text = _result_visible_text(result)
        leaked = [
            source_id
            for source_id in sorted(excluded_ids)
            if len(_normalized(str(source_by_id[source_id].get("text") or ""))) >= 2
            and _normalized(str(source_by_id[source_id].get("text") or "")).casefold() in visible_text
        ]
        if leaked:
            raise ValueError(f"Excluded notebook identity text is still visible: {leaked}")
    if layout_edit_level in {"preserve", "minor"} and order != sorted(order):
        raise ValueError(f"{layout_edit_level} layout mode does not allow source block reordering")


def bind_note_evidence_refs(
    result: ScholarNotesResult,
    completion_evidence: dict[str, dict],
) -> None:
    """Replace model-supplied source metadata with trusted backend values."""
    for block in result.note_ir.blocks:
        if block.origin != "evidence_supplement":
            continue
        bound = []
        for source_ref in block.source_refs:
            item = completion_evidence[source_ref.evidence_id]
            bound.append(
                NoteEvidenceRef(
                    evidence_id=item["id"],
                    source_type=item["source_type"],
                    document_id=item.get("document_id"),
                    knowledge_chunk_id=item.get("knowledge_chunk_id"),
                    question_id=item.get("question_id"),
                    grading_result_id=item.get("grading_result_id"),
                    page_index=item.get("page_index"),
                    block_id=item.get("block_id"),
                    excerpt=item.get("excerpt"),
                    relevance_score=item.get("relevance_score"),
                    authoritative=bool(item.get("authoritative")),
                )
            )
        block.source_refs = bound


def _result_visible_text(result: ScholarNotesResult) -> str:
    parts = [result.title, result.note_ir.summary, *result.outline, *result.review_suggestions]
    parts.extend(item.name for item in result.knowledge_points)
    for block in result.note_ir.blocks:
        parts.extend([block.text, block.latex or "", block.table_html or ""])
        parts.extend(relation.text or "" for relation in block.relations)
    return _normalized(" ".join(parts)).casefold()


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
    if block.type == "code":
        language = escape(block.language or "text")
        main = f'<pre class="np-code"><code data-language="{language}">{escape(block.text)}</code></pre>'
    elif block.type == "formula":
        main = f'<div class="np-formula">{escape(block.latex or block.text)}</div>'
    elif block.type == "table" and block.table_html:
        main = sanitize_note_html(block.table_html)
    elif block.type in {"annotation", "diagram"}:
        main = f'<aside class="np-annotation">{escape(block.text)}</aside>'
    else:
        main = render_note_markdown(block.text)
    if block.relations:
        items = []
        for relation in block.relations:
            label = relation.text or relation.type
            target = f" → {relation.target_block_id}" if relation.target_block_id else ""
            items.append(f'<li><span class="np-annotation-marker">{escape(label + target)}</span></li>')
        main += '<ul class="np-annotation">' + "".join(items) + "</ul>"
    if block.origin == "evidence_supplement":
        return (
            f'<div id="{block_id}" class="np-source-block np-evidence-supplement" '
            f'data-note-block-id="{block_id}">'
            '<span class="np-supplement-badge">资料补充</span>'
            f'{main}</div>'
        )
    return f'<div id="{block_id}" class="np-source-block">{main}</div>'


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
