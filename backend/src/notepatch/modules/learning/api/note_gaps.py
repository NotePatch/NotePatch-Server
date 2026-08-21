from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import (
    get_current_user,
    get_storage_service,
    get_task_service,
    get_workspace_member,
)
from notepatch.modules.documents.models.document import Document
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.models.learning import (
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.learning.models.note_workflow import (
    NoteGapSuggestion,
    NoteSupplementDraft,
    StudyNoteCorrection,
)
from notepatch.modules.learning.schemas.learning import (
    StudyNoteRevisionResponse,
    StudyNoteVersionRead,
)
from notepatch.modules.learning.schemas.note_workflow import (
    NoteDraftCreate,
    NoteDraftRegenerate,
    NoteDraftUpdate,
    NoteGapDetail,
    NoteGapRead,
    NoteSupplementDraftRead,
    NotesFromGapsRequest,
    StudyNoteCorrectionRead,
    StudyNoteGenerateRequest,
)
from notepatch.modules.learning.services.html_notes import sanitize_note_html
from notepatch.modules.learning.services.notes import StudyNoteService
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.database import get_db
from notepatch.platform.storage import StorageService


router = APIRouter(prefix="/workspaces/{workspace_id}/learning-units", tags=["note-workflow"])


def _unit(db: Session, workspace_id: str, unit_id: str) -> LearningUnit:
    unit = db.scalar(
        select(LearningUnit).where(
            LearningUnit.workspace_id == workspace_id,
            LearningUnit.id == unit_id,
            LearningUnit.merged_into_id.is_(None),
        )
    )
    if unit is None:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    return unit


def _gap(db: Session, workspace_id: str, unit_id: str, gap_id: str) -> NoteGapSuggestion:
    gap = db.scalar(
        select(NoteGapSuggestion).where(
            NoteGapSuggestion.workspace_id == workspace_id,
            NoteGapSuggestion.learning_unit_id == unit_id,
            NoteGapSuggestion.id == gap_id,
        )
    )
    if gap is None:
        raise HTTPException(status_code=404, detail="Note gap suggestion not found")
    return gap


def _latest_note(db: Session, workspace_id: str, unit_id: str) -> StudyNoteVersion | None:
    return db.scalar(
        select(StudyNoteVersion)
        .where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == unit_id,
        )
        .order_by(StudyNoteVersion.version_no.desc())
    )


def _selected_refs(gap: NoteGapSuggestion, requested: list[dict]) -> list[dict]:
    if not requested:
        return list(gap.source_refs or [])
    available = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in gap.source_refs or []}
    if any(json.dumps(item, sort_keys=True, ensure_ascii=False) not in available for item in requested):
        raise HTTPException(status_code=422, detail="Selected source is not part of this suggestion")
    return requested


def _insert_fragment(base_html: str, fragment_html: str, section_id: str | None, position: str) -> str:
    try:
        root = ET.fromstring(f"<np-root>{base_html}</np-root>")
        fragment_root = ET.fromstring(f"<np-fragment>{fragment_html}</np-fragment>")
    except ET.ParseError as exc:
        raise HTTPException(status_code=409, detail="Study note HTML cannot be revised safely") from exc
    nodes = list(fragment_root)
    if not nodes:
        raise HTTPException(status_code=422, detail="Supplement HTML is empty")
    target = next((node for node in root.iter() if section_id and node.attrib.get("id") == section_id), None)
    if target is None:
        target = next((node for node in root if node.tag in {"article", "section", "div"}), root)
        position = "inside"
    if position == "inside":
        for node in nodes:
            target.append(node)
    else:
        parent = next((candidate for candidate in root.iter() if target in list(candidate)), None)
        if parent is None:
            raise HTTPException(status_code=409, detail="Suggested note location no longer exists")
        index = list(parent).index(target) + (1 if position == "after" else 0)
        for node in nodes:
            parent.insert(index, node)
            index += 1
    return "".join(ET.tostring(child, encoding="unicode", method="html") for child in root)


@router.post(
    "/{learning_unit_id}/notes/generate",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_study_note(
    workspace_id: str,
    learning_unit_id: str,
    payload: StudyNoteGenerateRequest,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    tasks: TaskService = Depends(get_task_service),
) -> TaskRead:
    unit = _unit(db, workspace_id, learning_unit_id)
    note_document = db.scalar(
        select(Document.id)
        .join(LearningUnitDocument, LearningUnitDocument.document_id == Document.id)
        .where(
            LearningUnitDocument.workspace_id == workspace_id,
            LearningUnitDocument.learning_unit_id == learning_unit_id,
            Document.workspace_id == workspace_id,
            Document.document_kind == "note",
            Document.status.in_(("uploaded", "ready")),
        )
    )
    if note_document is None:
        raise HTTPException(status_code=409, detail="No usable note document exists in this learning unit")
    active = tasks.find_active_task(
        workspace_id=workspace_id,
        task_type="generate_study_notes",
        resource_type="learning_unit",
        resource_id=learning_unit_id,
    )
    if active is not None:
        if payload.force_reprocess:
            raise HTTPException(status_code=409, detail="Study note generation is already active")
        return active
    return tasks.create_task(
        workspace_id=workspace_id,
        task_type="generate_study_notes",
        resource_type="learning_unit",
        resource_id=learning_unit_id,
        payload={
            "learning_unit_id": learning_unit_id,
            "expected_knowledge_revision": unit.knowledge_revision,
            "reason": "manual_generation",
            "force_reprocess": payload.force_reprocess,
            "content_edit_level": payload.content_edit_level or current_user.note_content_edit_level,
            "layout_edit_level": payload.layout_edit_level or current_user.note_layout_edit_level,
            "history_limit": current_user.note_history_limit,
        },
    )


@router.get(
    "/{learning_unit_id}/notes/{note_version_id}/corrections",
    response_model=list[StudyNoteCorrectionRead],
)
def list_note_corrections(
    workspace_id: str,
    learning_unit_id: str,
    note_version_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[StudyNoteCorrection]:
    _unit(db, workspace_id, learning_unit_id)
    note = db.scalar(
        select(StudyNoteVersion.id).where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == learning_unit_id,
            StudyNoteVersion.id == note_version_id,
        )
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Study note not found")
    return db.scalars(
        select(StudyNoteCorrection)
        .where(
            StudyNoteCorrection.workspace_id == workspace_id,
            StudyNoteCorrection.learning_unit_id == learning_unit_id,
            StudyNoteCorrection.note_version_id == note_version_id,
        )
        .order_by(StudyNoteCorrection.created_at)
    ).all()


@router.get("/{learning_unit_id}/note-gaps", response_model=list[NoteGapRead])
def list_note_gaps(
    workspace_id: str,
    learning_unit_id: str,
    gap_status: str | None = Query(default=None, alias="status"),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[NoteGapSuggestion]:
    _unit(db, workspace_id, learning_unit_id)
    query = select(NoteGapSuggestion).where(
        NoteGapSuggestion.workspace_id == workspace_id,
        NoteGapSuggestion.learning_unit_id == learning_unit_id,
    )
    if gap_status:
        query = query.where(NoteGapSuggestion.status == gap_status)
    return db.scalars(query.order_by(NoteGapSuggestion.updated_at.desc())).all()


@router.get("/{learning_unit_id}/note-gaps/{gap_id}", response_model=NoteGapDetail)
def get_note_gap(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> NoteGapDetail:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    drafts = db.scalars(
        select(NoteSupplementDraft)
        .where(
            NoteSupplementDraft.workspace_id == workspace_id,
            NoteSupplementDraft.learning_unit_id == learning_unit_id,
            NoteSupplementDraft.gap_suggestion_id == gap_id,
        )
        .order_by(NoteSupplementDraft.version_no.desc())
    ).all()
    return NoteGapDetail(
        suggestion=NoteGapRead.model_validate(gap),
        drafts=[NoteSupplementDraftRead.model_validate(item) for item in drafts],
    )


def _create_draft(
    *,
    db: Session,
    tasks: TaskService,
    gap: NoteGapSuggestion,
    refs: list[dict],
    section_id: str | None,
    position: str,
    instruction: str | None,
    feedback: str | None,
) -> TaskRead:
    latest = _latest_note(db, gap.workspace_id, gap.learning_unit_id)
    if gap.note_version_id and (latest is None or latest.id != gap.note_version_id):
        gap.status = "stale"
        db.commit()
        raise HTTPException(status_code=409, detail="The base note changed; refresh suggestions")
    version_no = int(
        db.scalar(
            select(func.coalesce(func.max(NoteSupplementDraft.version_no), 0)).where(
                NoteSupplementDraft.gap_suggestion_id == gap.id
            )
        )
        or 0
    ) + 1
    draft = NoteSupplementDraft(
        workspace_id=gap.workspace_id,
        learning_unit_id=gap.learning_unit_id,
        gap_suggestion_id=gap.id,
        base_note_version_id=latest.id if latest else None,
        version_no=version_no,
        status="queued",
        selected_source_refs=refs,
        target_section_id=section_id,
        target_anchor=f"#{section_id}" if section_id else None,
        insert_position=position,
        instruction=instruction,
        feedback=feedback,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return tasks.create_task(
        workspace_id=gap.workspace_id,
        task_type="generate_note_supplement",
        resource_type="note_gap",
        resource_id=draft.id,
        payload={
            "learning_unit_id": gap.learning_unit_id,
            "gap_id": gap.id,
            "draft_id": draft.id,
            "base_note_version_id": draft.base_note_version_id,
        },
    )


@router.post(
    "/{learning_unit_id}/note-gaps/{gap_id}/draft",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_gap_draft(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    payload: NoteDraftCreate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    tasks: TaskService = Depends(get_task_service),
) -> TaskRead:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    if gap.status in {"stale", "accepted", "rejected"}:
        raise HTTPException(status_code=409, detail="Note gap suggestion is no longer actionable")
    return _create_draft(
        db=db,
        tasks=tasks,
        gap=gap,
        refs=_selected_refs(gap, payload.selected_source_refs),
        section_id=payload.target_section_id or gap.target_section_id,
        position=payload.insert_position,
        instruction=payload.instruction,
        feedback=None,
    )


@router.patch(
    "/{learning_unit_id}/note-gaps/{gap_id}/draft",
    response_model=NoteSupplementDraftRead,
)
def update_gap_draft(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    payload: NoteDraftUpdate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> NoteSupplementDraft:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    draft = db.scalar(
        select(NoteSupplementDraft)
        .where(
            NoteSupplementDraft.workspace_id == workspace_id,
            NoteSupplementDraft.learning_unit_id == learning_unit_id,
            NoteSupplementDraft.gap_suggestion_id == gap.id,
        )
        .order_by(NoteSupplementDraft.version_no.desc())
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="Supplement draft not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field is required")
    if "html" in changes:
        draft.html = sanitize_note_html(changes["html"])
        draft.status = "ready"
    if "target_section_id" in changes:
        draft.target_section_id = changes["target_section_id"]
        draft.target_anchor = f"#{changes['target_section_id']}" if changes["target_section_id"] else None
    if "insert_position" in changes:
        draft.insert_position = changes["insert_position"]
    db.commit()
    db.refresh(draft)
    return draft


@router.post(
    "/{learning_unit_id}/note-gaps/{gap_id}/draft/regenerate",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_gap_draft(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    payload: NoteDraftRegenerate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    tasks: TaskService = Depends(get_task_service),
) -> TaskRead:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    previous = db.scalar(
        select(NoteSupplementDraft)
        .where(
            NoteSupplementDraft.workspace_id == workspace_id,
            NoteSupplementDraft.learning_unit_id == learning_unit_id,
            NoteSupplementDraft.gap_suggestion_id == gap.id,
        )
        .order_by(NoteSupplementDraft.version_no.desc())
    )
    if previous is None:
        raise HTTPException(status_code=404, detail="Supplement draft not found")
    return _create_draft(
        db=db,
        tasks=tasks,
        gap=gap,
        refs=list(previous.selected_source_refs or gap.source_refs or []),
        section_id=previous.target_section_id,
        position=previous.insert_position,
        instruction=previous.instruction,
        feedback=payload.feedback,
    )


@router.post(
    "/{learning_unit_id}/note-gaps/{gap_id}/accept",
    response_model=StudyNoteRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def accept_gap_draft(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> StudyNoteRevisionResponse:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    draft = db.scalar(
        select(NoteSupplementDraft)
        .where(
            NoteSupplementDraft.workspace_id == workspace_id,
            NoteSupplementDraft.learning_unit_id == learning_unit_id,
            NoteSupplementDraft.gap_suggestion_id == gap.id,
            NoteSupplementDraft.status == "ready",
        )
        .order_by(NoteSupplementDraft.version_no.desc())
    )
    if draft is None:
        raise HTTPException(status_code=409, detail="No ready supplement draft exists")
    latest = _latest_note(db, workspace_id, learning_unit_id)
    if latest is None:
        raise HTTPException(status_code=409, detail="Create the first note from selected gaps before accepting")
    if draft.base_note_version_id != latest.id or gap.note_version_id != latest.id:
        gap.status = "stale"
        db.commit()
        raise HTTPException(status_code=409, detail="The base note changed; regenerate the supplement")
    base_html = storage.get_text_artifact(latest.html_object_key, bucket=storage.bucket)
    merged = _insert_fragment(
        base_html,
        draft.html,
        draft.target_section_id,
        draft.insert_position,
    )
    source_ids = list(latest.source_document_ids or [])
    source_ids.extend(
        ref.get("document_id") for ref in draft.selected_source_refs if ref.get("document_id")
    )
    note, downstream = StudyNoteService(db, storage).create_revision(
        workspace_id=workspace_id,
        learning_unit_id=learning_unit_id,
        base_version_id=latest.id,
        actor=current_user,
        html=merged,
        title=None,
        edit_summary=f"Added missing knowledge point {gap.knowledge_point_id}",
        edit_origin="gap_suggestion",
        knowledge_point_ids=[*(latest.knowledge_point_ids or []), gap.knowledge_point_id],
        source_document_ids=source_ids,
    )
    gap.status = "accepted"
    gap.accepted_version_id = note.id
    draft.status = "accepted"
    for item in db.scalars(
        select(NoteGapSuggestion).where(
            NoteGapSuggestion.workspace_id == workspace_id,
            NoteGapSuggestion.learning_unit_id == learning_unit_id,
            NoteGapSuggestion.status.in_(("pending", "draft", "no_base_note")),
            NoteGapSuggestion.id != gap.id,
        )
    ).all():
        item.status = "stale"
    db.commit()
    return StudyNoteRevisionResponse(
        note=StudyNoteVersionRead.model_validate(note),
        downstream_tasks=[
            {"id": task.id, "task_type": task.task_type, "status": task.status}
            for task in downstream
        ],
    )


@router.post("/{learning_unit_id}/note-gaps/{gap_id}/reject", response_model=NoteGapRead)
def reject_note_gap(
    workspace_id: str,
    learning_unit_id: str,
    gap_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> NoteGapSuggestion:
    _unit(db, workspace_id, learning_unit_id)
    gap = _gap(db, workspace_id, learning_unit_id, gap_id)
    if gap.status == "accepted":
        raise HTTPException(status_code=409, detail="Accepted suggestion cannot be rejected")
    gap.status = "rejected"
    db.commit()
    db.refresh(gap)
    return gap


@router.post(
    "/{learning_unit_id}/notes/from-gaps",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_note_from_gaps(
    workspace_id: str,
    learning_unit_id: str,
    payload: NotesFromGapsRequest,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    tasks: TaskService = Depends(get_task_service),
) -> TaskRead:
    unit = _unit(db, workspace_id, learning_unit_id)
    if _latest_note(db, workspace_id, learning_unit_id) is not None:
        raise HTTPException(status_code=409, detail="A study note already exists; use gap drafts")
    gap_ids = list(dict.fromkeys(payload.gap_ids))
    gaps = db.scalars(
        select(NoteGapSuggestion).where(
            NoteGapSuggestion.workspace_id == workspace_id,
            NoteGapSuggestion.learning_unit_id == learning_unit_id,
            NoteGapSuggestion.id.in_(gap_ids),
            NoteGapSuggestion.status == "no_base_note",
        )
    ).all()
    if len(gaps) != len(gap_ids):
        raise HTTPException(status_code=404, detail="One or more note gap suggestions were not found")
    source_blocks = []
    for gap in gaps:
        for index, ref in enumerate(gap.source_refs or []):
            source_blocks.append(
                {
                    "id": f"gap:{gap.id}:{index}",
                    "document_id": ref.get("document_id") or "knowledge-base",
                    "page_index": int(ref.get("page_index") or 0),
                    "bbox": ref.get("bbox") or [0, 0, 1, 1],
                    "reading_order": len(source_blocks) + 1,
                    "type": "text",
                    "text": ref.get("excerpt") or (gap.metadata_ or {}).get("knowledge_point_name") or "",
                    "confidence": 1.0,
                }
            )
    if not source_blocks:
        raise HTTPException(status_code=409, detail="Selected suggestions have no usable sources")
    return tasks.create_task(
        workspace_id=workspace_id,
        task_type="generate_study_notes",
        resource_type="learning_unit",
        resource_id=learning_unit_id,
        payload={
            "learning_unit_id": learning_unit_id,
            "gap_ids": gap_ids,
            "source_blocks": source_blocks,
            "title": payload.title,
            "expected_knowledge_revision": unit.knowledge_revision,
            "reason": "explicit_from_gaps",
            "content_edit_level": payload.content_edit_level or current_user.note_content_edit_level,
            "layout_edit_level": payload.layout_edit_level or current_user.note_layout_edit_level,
            "history_limit": current_user.note_history_limit,
        },
    )
