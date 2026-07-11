from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.platform.database import utcnow
from notepatch.platform.storage import StorageService
from notepatch.shared.filenames import extension_for_filename, sanitize_filename


MIRRORABLE_DOCUMENT_STATUSES = {"uploaded", "ready"}


class OpenClawRuntimeMirror:
    def sync_workspace_documents(
        self,
        *,
        db: Session,
        storage: StorageService,
        workspace_id: str,
        task_id: str,
    ) -> dict:
        runtime = self.runtime_for_workspace(db, workspace_id)
        user_id = runtime["user_id"]
        documents_root = self.documents_root(user_id)
        if documents_root.exists():
            shutil.rmtree(documents_root)
        documents_root.mkdir(parents=True, exist_ok=True)

        documents = db.scalars(
            select(Document)
            .where(Document.workspace_id == workspace_id, Document.status != "deleted")
            .order_by(Document.created_at.asc())
        ).all()
        index_documents: list[dict] = []
        skipped_documents: list[dict] = []
        skipped_artifacts: list[dict] = []
        file_count = 0
        for document in documents:
            reason = self._document_skip_reason(document, workspace_id)
            if reason is not None:
                skipped_documents.append(self._skipped_document(document, reason))
                continue
            document_dir = documents_root / document.id
            original_dir = document_dir / "original"
            artifacts_dir = document_dir / "artifacts"
            ocr_dir = document_dir / "ocr"
            for path in (original_dir, artifacts_dir, ocr_dir):
                path.mkdir(parents=True, exist_ok=True)

            original_path = original_dir / sanitize_filename(document.original_filename)
            try:
                storage.download_file(document.bucket, document.object_key, original_path)
            except Exception as exc:
                if StorageService.is_object_not_found_error(exc):
                    shutil.rmtree(document_dir, ignore_errors=True)
                    skipped_documents.append(self._skipped_document(document, "object_not_found"))
                    continue
                from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

                raise OpenClawUserRuntimeError(
                    f"Could not mirror document {document.id} object {document.object_key}: {exc}"
                ) from exc
            file_count += 1

            artifact_entries: list[dict] = []
            ocr_markdown_path = None
            ocr_text_path = None
            for artifact in sorted(document.artifacts, key=lambda item: item.created_at):
                artifact_reason = self._artifact_skip_reason(artifact)
                if artifact_reason is not None:
                    skipped_artifacts.append(self._skipped_artifact(artifact, document.id, artifact_reason))
                    continue
                artifact_path = self._mirror_artifact_path(
                    artifact,
                    artifacts_dir=artifacts_dir,
                    ocr_dir=ocr_dir,
                )
                try:
                    storage.download_file(artifact.bucket, artifact.object_key, artifact_path)
                except Exception as exc:
                    if StorageService.is_object_not_found_error(exc):
                        skipped_artifacts.append(
                            self._skipped_artifact(artifact, document.id, "object_not_found")
                        )
                        continue
                    from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

                    raise OpenClawUserRuntimeError(
                        f"Could not mirror artifact {artifact.id} object {artifact.object_key}: {exc}"
                    ) from exc
                file_count += 1
                container_path = self._container_workspace_path(user_id, artifact_path)
                if artifact.artifact_type == "ocr_markdown":
                    ocr_markdown_path = container_path
                elif artifact.artifact_type == "ocr_text":
                    ocr_text_path = container_path
                artifact_entries.append(
                    {
                        "id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "bucket": artifact.bucket,
                        "object_key": artifact.object_key,
                        "mime_type": artifact.mime_type,
                        "file_size": artifact.file_size,
                        "metadata": artifact.metadata_,
                        "created_at": self._iso(artifact.created_at),
                        "path": container_path,
                    }
                )

            index_documents.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "original_filename": document.original_filename,
                    "mime_type": document.mime_type,
                    "file_size": document.file_size,
                    "file_type": document.file_type,
                    "document_kind": document.document_kind,
                    "status": document.status,
                    "bucket": document.bucket,
                    "object_key": document.object_key,
                    "metadata": document.metadata_,
                    "created_at": self._iso(document.created_at),
                    "updated_at": self._iso(document.updated_at),
                    "original_path": self._container_workspace_path(user_id, original_path),
                    "ocr_markdown_path": ocr_markdown_path,
                    "ocr_text_path": ocr_text_path,
                    "artifacts": artifact_entries,
                }
            )

        task_input_dir = self.task_input_dir(user_id, task_id)
        task_output_dir = self.task_output_dir(user_id, task_id)
        task_input_dir.mkdir(parents=True, exist_ok=True)
        task_output_dir.mkdir(parents=True, exist_ok=True)
        index_path = documents_root / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "generated_at": self._iso(utcnow()),
                    "documents_root": "/workspace/notepatch/documents",
                    "task_output_dir": f"/workspace/notepatch/openclaw/tasks/{task_id}/output",
                    "documents": index_documents,
                    "skipped_documents": skipped_documents,
                    "skipped_artifacts": skipped_artifacts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._ensure_runtime_permissions(user_id, roots=(self.workspace_dir(user_id),))
        return {
            **runtime,
            "documents_index_path": "/workspace/notepatch/documents/index.json",
            "documents_root_path": "/workspace/notepatch/documents",
            "task_output_path": f"/workspace/notepatch/openclaw/tasks/{task_id}/output",
            "host_task_output_dir": str(task_output_dir),
            "host_task_input_dir": str(task_input_dir),
            "documents_synced": len(index_documents),
            "files_synced": file_count,
            "documents_skipped": len(skipped_documents),
            "artifacts_skipped": len(skipped_artifacts),
            "skipped_documents": skipped_documents,
            "skipped_artifacts": skipped_artifacts,
        }

    @staticmethod
    def _document_skip_reason(document: Document, workspace_id: str) -> str | None:
        if document.status not in MIRRORABLE_DOCUMENT_STATUSES:
            return "status_not_mirrorable"
        if document.file_size is None:
            return "missing_file_size"
        if not document.bucket or not document.object_key:
            return "missing_storage_location"
        if not document.object_key.startswith(f"workspaces/{workspace_id}/documents/{document.id}/"):
            return "object_key_outside_workspace"
        return None

    @staticmethod
    def _artifact_skip_reason(artifact: DocumentArtifact) -> str | None:
        return None if artifact.bucket and artifact.object_key else "missing_storage_location"

    @staticmethod
    def _skipped_document(document: Document, reason: str) -> dict:
        return {
            "id": document.id,
            "title": document.title,
            "original_filename": document.original_filename,
            "status": document.status,
            "bucket": document.bucket,
            "object_key": document.object_key,
            "file_size": document.file_size,
            "mime_type": document.mime_type,
            "reason": reason,
        }

    @staticmethod
    def _skipped_artifact(artifact: DocumentArtifact, document_id: str, reason: str) -> dict:
        return {
            "id": artifact.id,
            "document_id": document_id,
            "artifact_type": artifact.artifact_type,
            "bucket": artifact.bucket,
            "object_key": artifact.object_key,
            "file_size": artifact.file_size,
            "mime_type": artifact.mime_type,
            "reason": reason,
        }

    @staticmethod
    def _artifact_filename(artifact: DocumentArtifact) -> str:
        object_name = PurePosixPath(artifact.object_key).name
        if object_name:
            safe = sanitize_filename(object_name)
            if safe != "upload.bin":
                return f"{artifact.artifact_type}-{artifact.id}-{safe}"
        ext = extension_for_filename(f"artifact.{artifact.mime_type or 'bin'}")
        return sanitize_filename(f"{artifact.artifact_type}-{artifact.id}.{ext}")

    def _mirror_artifact_path(
        self,
        artifact: DocumentArtifact,
        *,
        artifacts_dir: Path,
        ocr_dir: Path,
    ) -> Path:
        if artifact.artifact_type == "ocr_markdown":
            return ocr_dir / "ocr.md"
        if artifact.artifact_type == "ocr_text":
            return ocr_dir / "ocr.txt"
        return artifacts_dir / self._artifact_filename(artifact)

    def _container_workspace_path(self, user_id: str, host_path: Path) -> str:
        relative = host_path.relative_to(self.workspace_dir(user_id))
        return "/" + str(PurePosixPath("workspace", *relative.parts))
