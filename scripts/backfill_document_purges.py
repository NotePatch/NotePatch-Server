from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from notepatch.platform.database import SessionLocal  # noqa: E402
from notepatch.modules.documents.models.document import Document  # noqa: E402
from notepatch.modules.documents.services.purge import DocumentPurgeService  # noqa: E402
from notepatch.platform.storage import StorageService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue purge tasks for legacy documents already marked deleted."
    )
    parser.add_argument("--workspace-id", help="Limit the backfill to one personal workspace.")
    parser.add_argument("--apply", action="store_true", help="Create purge tasks. Default is dry-run.")
    args = parser.parse_args()

    with SessionLocal() as db:
        query = select(Document).where(
            Document.status == "deleted",
            (Document.purge_status.is_(None)) | (Document.purge_status == "failed"),
        )
        if args.workspace_id:
            query = query.where(Document.workspace_id == args.workspace_id)
        documents = db.scalars(query.order_by(Document.created_at.asc())).all()
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode}: {len(documents)} legacy deleted document(s)")
        if not args.apply:
            for document in documents:
                print(f"candidate workspace_id={document.workspace_id} document_id={document.id}")
            print("No purge tasks created. Re-run with --apply to queue them.")
            return

        service = DocumentPurgeService(db, StorageService())
        for document in documents:
            try:
                updated, task = service.request_purge(document.workspace_id, document.id)
                print(
                    f"queued workspace_id={updated.workspace_id} document_id={updated.id} "
                    f"purge_task_id={task.id} purge_status={updated.purge_status}"
                )
            except Exception as exc:
                db.rollback()
                print(
                    f"failed workspace_id={document.workspace_id} document_id={document.id} error={exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
