from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from notepatch.modules.documents.ocr.base import OcrOptions
from notepatch.modules.documents.ocr.image_preprocessor import image_size


SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/tiff"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


class OcrUnsupportedDocumentError(RuntimeError):
    pass


class OcrDocumentTooLargeError(RuntimeError):
    pass


@dataclass
class RenderedPage:
    page_index: int
    path: Path
    width: int
    height: int
    rotation: int = 0


class DocumentRenderer:
    def render(
        self,
        source_path: Path,
        output_dir: Path,
        *,
        mime_type: str | None,
        file_type: str,
        options: OcrOptions,
    ) -> list[RenderedPage]:
        self._validate_file_size(source_path, options)
        normalized_mime = (mime_type or "").lower()
        suffix = source_path.suffix.lower()
        if file_type == "pdf" or normalized_mime == "application/pdf" or suffix == ".pdf":
            return self._render_pdf(source_path, output_dir, options=options)
        if file_type == "image" or normalized_mime in SUPPORTED_IMAGE_MIME_TYPES or suffix in SUPPORTED_IMAGE_EXTENSIONS:
            return [self._render_image(source_path, output_dir)]
        if file_type in {"docx", "pptx"}:
            raise OcrUnsupportedDocumentError("Document must be converted to PDF or image before OCR")
        raise OcrUnsupportedDocumentError(f"Unsupported OCR input type: {mime_type or file_type or suffix}")

    @staticmethod
    def _validate_file_size(source_path: Path, options: OcrOptions) -> None:
        max_bytes = options.max_file_size_mb * 1024 * 1024
        if max_bytes > 0 and source_path.stat().st_size > max_bytes:
            raise OcrDocumentTooLargeError(
                f"OCR input is larger than OCR_MAX_FILE_SIZE_MB={options.max_file_size_mb}"
            )

    def _render_image(self, source_path: Path, output_dir: Path) -> RenderedPage:
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix if source_path.suffix else ".img"
        page_path = output_dir / f"page_0001{suffix}"
        if source_path != page_path:
            shutil.copyfile(source_path, page_path)
        width, height = image_size(page_path)
        return RenderedPage(page_index=0, path=page_path, width=width, height=height)

    def _render_pdf(self, source_path: Path, output_dir: Path, *, options: OcrOptions) -> list[RenderedPage]:
        if options.max_pages < 1:
            raise OcrDocumentTooLargeError("PDF page count exceeds OCR_MAX_PAGES=0")
        try:
            import fitz  # type: ignore
        except Exception as exc:
            raise OcrUnsupportedDocumentError(f"PyMuPDF is required to render PDFs: {exc}") from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with fitz.open(str(source_path)) as pdf:
                page_count = pdf.page_count
                if page_count > options.max_pages:
                    raise OcrDocumentTooLargeError(
                        f"PDF has {page_count} pages, exceeding OCR_MAX_PAGES={options.max_pages}"
                    )
                scale = options.render_dpi / 72.0
                matrix = fitz.Matrix(scale, scale)
                pages: list[RenderedPage] = []
                for page_index in range(page_count):
                    page = pdf.load_page(page_index)
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                    page_path = output_dir / f"page_{page_index + 1:04d}.png"
                    pixmap.save(str(page_path))
                    pages.append(
                        RenderedPage(
                            page_index=page_index,
                            path=page_path,
                            width=int(pixmap.width),
                            height=int(pixmap.height),
                            rotation=int(page.rotation or 0),
                        )
                    )
                return pages
        except OcrDocumentTooLargeError:
            raise
        except Exception as exc:
            raise OcrUnsupportedDocumentError(f"Could not render PDF for OCR: {exc}") from exc
