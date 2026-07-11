from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from notepatch.platform.database import SessionLocal, utcnow  # noqa: E402
from notepatch.modules.documents.models.document import Document, DocumentArtifact  # noqa: E402
from notepatch.modules.documents.models.upload import UploadSession  # noqa: E402
from notepatch.platform.storage import StorageService  # noqa: E402


INVALID_UPLOAD_STATUSES = {"created", "uploading", "failed"}


@dataclass(frozen=True)
class InvalidDocumentCandidate:
    document_id: str
    workspace_id: str
    status: str
    bucket: str
    object_key: str
    reasons: tuple[str, ...]


def find_invalid_documents(
    *,
    db: Session,
    storage: StorageService,
    workspace_id: str,
    old_object_key_workspace_id: str | None = None,
    older_than_minutes: int = 30,
) -> tuple[list[InvalidDocumentCandidate], list[dict]]:
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    documents = db.scalars(
        select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.status != "deleted",
            Document.created_at <= cutoff,
        )
        .order_by(Document.created_at.asc())
    ).all()
    candidates: list[InvalidDocumentCandidate] = []
    storage_errors: list[dict] = []
    for document in documents:
        reasons = _invalid_reasons(document, old_object_key_workspace_id)
        if not reasons:
            continue
        object_missing = False
        if not document.bucket or not document.object_key:
            object_missing = True
        else:
            try:
                object_missing = not storage.object_exists(document.bucket, document.object_key)
            except Exception as exc:
                storage_errors.append(
                    {
                        "document_id": document.id,
                        "bucket": document.bucket,
                        "object_key": document.object_key,
                        "error": str(exc),
                    }
                )
                continue
        if object_missing:
            candidates.append(
                InvalidDocumentCandidate(
                    document_id=document.id,
                    workspace_id=document.workspace_id,
                    status=document.status,
                    bucket=document.bucket,
                    object_key=document.object_key,
                    reasons=tuple(reasons),
                )
            )
    return candidates, storage_errors


def delete_invalid_documents(*, db: Session, candidates: list[InvalidDocumentCandidate]) -> dict[str, int]:
    document_ids = [candidate.document_id for candidate in candidates]
    if not document_ids:
        return {"documents": 0, "artifacts": 0, "upload_sessions": 0}
    upload_sessions = db.execute(delete(UploadSession).where(UploadSession.document_id.in_(document_ids))).rowcount or 0
    artifacts = db.execute(delete(DocumentArtifact).where(DocumentArtifact.document_id.in_(document_ids))).rowcount or 0
    documents = db.execute(delete(Document).where(Document.id.in_(document_ids))).rowcount or 0
    db.commit()
    return {
        "documents": documents,
        "artifacts": artifacts,
        "upload_sessions": upload_sessions,
    }


def _invalid_reasons(document: Document, old_object_key_workspace_id: str | None) -> list[str]:
    reasons: list[str] = []
    if document.status in INVALID_UPLOAD_STATUSES:
        reasons.append(f"status:{document.status}")
    if document.file_size is None:
        reasons.append("missing_file_size")
    if document.mime_type is None:
        reasons.append("missing_mime_type")
    if not document.bucket or not document.object_key:
        reasons.append("missing_storage_location")
    if old_object_key_workspace_id and old_object_key_workspace_id in (document.object_key or ""):
        reasons.append("old_workspace_object_key")
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete invalid NotePatch document metadata after confirming objects are absent.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--old-object-key-workspace-id")
    parser.add_argument("--older-than-minutes", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Physically delete matching DB records. Default is dry-run.")
    args = parser.parse_args()

    storage = StorageService()
    with SessionLocal() as db:
        candidates, storage_errors = find_invalid_documents(
            db=db,
            storage=storage,
            workspace_id=args.workspace_id,
            old_object_key_workspace_id=args.old_object_key_workspace_id,
            older_than_minutes=args.older_than_minutes,
        )
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode}: {len(candidates)} invalid document candidate(s)")
        for candidate in candidates:
            print(
                "candidate "
                f"document_id={candidate.document_id} "
                f"status={candidate.status} "
                f"object_key={candidate.object_key} "
                f"reasons={','.join(candidate.reasons)}"
            )
        for error in storage_errors:
            print(
                "storage-check-skipped "
                f"document_id={error['document_id']} "
                f"object_key={error['object_key']} "
                f"error={error['error']}"
            )
        if args.apply:
            deleted = delete_invalid_documents(db=db, candidates=candidates)
            print(
                "deleted "
                f"documents={deleted['documents']} "
                f"artifacts={deleted['artifacts']} "
                f"upload_sessions={deleted['upload_sessions']}"
            )
        else:
            print("No records deleted. Re-run with --apply to delete candidates.")


if __name__ == "__main__":
    main()
