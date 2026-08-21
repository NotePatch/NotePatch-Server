from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_VISUAL_ATTACHMENTS = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
DIRECT_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass(frozen=True)
class VisualAttachmentBuildResult:
    attachments: list[dict]
    document_ids: list[str]
    skipped: list[dict]
    used_previews: bool


class VisualAttachmentBuilder:
    """Build task-local multimodal attachments without persisting derivatives."""

    def build(self, runtime: dict, document_ids: list[str]) -> VisualAttachmentBuildResult:
        requested_ids = list(dict.fromkeys(document_ids))[-MAX_VISUAL_ATTACHMENTS:]
        contexts = runtime.get("document_contexts")
        host_input_dir = runtime.get("host_task_input_dir")
        container_documents_root = runtime.get("documents_root_path")
        if not isinstance(contexts, dict) or not isinstance(host_input_dir, str):
            return VisualAttachmentBuildResult([], [], self._skip_all(requested_ids, "snapshot_unavailable"), False)
        if not isinstance(container_documents_root, str):
            return VisualAttachmentBuildResult([], [], self._skip_all(requested_ids, "snapshot_unavailable"), False)

        host_documents_root = (Path(host_input_dir) / "documents").resolve()
        container_root = PurePosixPath(container_documents_root)
        candidates: list[tuple[dict, Path]] = []
        skipped: list[dict] = []
        for document_id in requested_ids:
            context = contexts.get(document_id)
            if not isinstance(context, dict) or context.get("file_type") != "image":
                skipped.append({"document_id": document_id, "reason": "image_context_unavailable"})
                continue
            original_path = context.get("original_path")
            if not isinstance(original_path, str):
                skipped.append({"document_id": document_id, "reason": "image_path_unavailable"})
                continue
            try:
                relative_path = PurePosixPath(original_path).relative_to(container_root)
            except ValueError:
                skipped.append({"document_id": document_id, "reason": "image_path_outside_snapshot"})
                continue
            source_path = (host_documents_root / Path(*relative_path.parts)).resolve()
            if not source_path.is_relative_to(host_documents_root):
                skipped.append({"document_id": document_id, "reason": "image_path_outside_snapshot"})
                continue
            if not source_path.is_file():
                skipped.append({"document_id": document_id, "reason": "image_snapshot_missing"})
                continue
            if not self._is_decodable_image(source_path):
                skipped.append({"document_id": document_id, "reason": "image_decode_failed"})
                continue
            candidates.append((context, source_path))

        if not candidates:
            return VisualAttachmentBuildResult([], [], skipped, False)

        direct_sizes = [path.stat().st_size for _, path in candidates]
        direct_compatible = all(
            context.get("mime_type") in DIRECT_IMAGE_MIME_TYPES and size <= MAX_IMAGE_BYTES
            for (context, _), size in zip(candidates, direct_sizes, strict=True)
        )
        if direct_compatible and sum(direct_sizes) <= MAX_TOTAL_BYTES:
            attachments = [self._attachment(context, context["original_path"]) for context, _ in candidates]
            return VisualAttachmentBuildResult(
                attachments,
                [item["document_id"] for item in attachments],
                skipped,
                False,
            )

        preview_dir = host_documents_root / ".visual-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        per_image_budget = min(MAX_IMAGE_BYTES, MAX_TOTAL_BYTES // max(len(candidates), 1))
        attachments: list[dict] = []
        for context, source_path in candidates:
            document_id = context["document_id"]
            preview_path = preview_dir / f"{document_id}.jpg"
            try:
                self._write_preview(source_path, preview_path, per_image_budget)
            except (OSError, UnidentifiedImageError, ValueError):
                skipped.append({"document_id": document_id, "reason": "preview_generation_failed"})
                continue
            container_preview_path = str(container_root / ".visual-previews" / preview_path.name)
            preview_context = {
                **context,
                "filename": f"visual-{document_id}.jpg",
                "mime_type": "image/jpeg",
            }
            attachments.append(self._attachment(preview_context, container_preview_path))

        return VisualAttachmentBuildResult(
            attachments,
            [item["document_id"] for item in attachments],
            skipped,
            True,
        )

    @staticmethod
    def _attachment(context: dict, path: str) -> dict:
        return {
            "document_id": context["document_id"],
            "filename": context.get("filename") or f"{context['document_id']}.jpg",
            "title": context.get("title"),
            "mime_type": context.get("mime_type") or "image/jpeg",
            "file_type": "image",
            "original_path": path,
            "purpose": "layout_reference",
        }

    @staticmethod
    def _is_decodable_image(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            return False

    @staticmethod
    def _write_preview(source_path: Path, destination: Path, byte_budget: int) -> None:
        with Image.open(source_path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            for max_dimension in (2048, 1792, 1536, 1280, 1024, 768):
                resized = image.copy()
                resized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                for quality in (88, 80, 72, 64, 56):
                    resized.save(destination, format="JPEG", quality=quality, optimize=True)
                    if destination.stat().st_size <= byte_budget:
                        return
        destination.unlink(missing_ok=True)
        raise ValueError("Could not fit visual preview within the gateway byte budget")

    @staticmethod
    def _skip_all(document_ids: list[str], reason: str) -> list[dict]:
        return [{"document_id": document_id, "reason": reason} for document_id in document_ids]
