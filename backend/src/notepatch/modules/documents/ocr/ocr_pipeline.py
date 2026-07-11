from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from notepatch.platform.config import Settings, get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.documents.ocr.base import OcrDocumentResult, OcrEngine, OcrOptions, OcrPageResult
from notepatch.modules.documents.ocr.document_renderer import DocumentRenderer
from notepatch.modules.documents.ocr.image_preprocessor import ImagePreprocessor, image_size
from notepatch.modules.documents.ocr.markdown_exporter import MarkdownExporter
from notepatch.modules.documents.ocr.paddle_structure_engine import PaddleStructureEngine


OcrEventCallback = Callable[[str, str, dict[str, Any]], None]


class OcrPipelineError(RuntimeError):
    pass


class OcrPipeline:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        renderer: DocumentRenderer | None = None,
        preprocessor: ImagePreprocessor | None = None,
        exporter: MarkdownExporter | None = None,
        engine: OcrEngine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.renderer = renderer or DocumentRenderer()
        self.preprocessor = preprocessor or ImagePreprocessor()
        self.exporter = exporter or MarkdownExporter()
        self.engine = engine

    def run(
        self,
        *,
        input_path: Path,
        document_id: str,
        workspace_id: str,
        source: dict[str, Any],
        mime_type: str | None,
        file_type: str,
        event_callback: OcrEventCallback | None = None,
    ) -> OcrDocumentResult:
        options = self._options()
        if self.engine is None and options.engine.strip().lower() != "paddleocr":
            raise OcrPipelineError(
                f"Unsupported production OCR_ENGINE={options.engine!r}; only 'paddleocr' is allowed"
            )
        engine = self.engine or PaddleStructureEngine(options)
        runtime_options = options

        event_callback = event_callback or (lambda _event, _message, _data: None)
        event_callback(
            "ocr_started",
            "OCR started",
            {"engine": engine.name, "input_path": str(input_path)},
        )

        with tempfile.TemporaryDirectory(prefix="notepatch-ocr-pages-", dir=_safe_temp_root(options.temp_dir)) as tmpdir:
            workdir = Path(tmpdir)
            rendered_pages = self.renderer.render(
                input_path,
                workdir / "rendered",
                mime_type=mime_type,
                file_type=file_type,
                options=runtime_options,
            )
            event_callback(
                "ocr_rendered",
                "OCR input rendered",
                {
                    "pages": len(rendered_pages),
                    "render_dpi": options.render_dpi,
                    "input_mime_type": mime_type,
                    "input_file_type": file_type,
                },
            )

            pages: list[OcrPageResult] = []
            block_counter = 1
            for rendered in rendered_pages:
                preprocessed_path = workdir / "preprocessed" / f"page_{rendered.page_index + 1:04d}{rendered.path.suffix}"
                self.preprocessor.preprocess(rendered.path, preprocessed_path, options=runtime_options)
                page = engine.recognize(preprocessed_path, page_index=rendered.page_index, options=runtime_options)
                page.width, page.height = image_size(preprocessed_path)
                if page.width <= 1 and rendered.width > 1:
                    page.width = rendered.width
                if page.height <= 1 and rendered.height > 1:
                    page.height = rendered.height
                page.rotation = rendered.rotation
                for order, block in enumerate(sorted(page.blocks, key=lambda item: item.reading_order), start=1):
                    block.id = f"block_{block_counter:04d}"
                    block.reading_order = order
                    block_counter += 1
                page.plain_text = self.exporter.page_plain_text(page)
                page.markdown = self.exporter.page_markdown(page)
                pages.append(page)
                event_callback(
                    "ocr_page_completed",
                    f"OCR page {rendered.page_index + 1} completed",
                    {
                        "page_index": rendered.page_index,
                        "blocks": len(page.blocks),
                        "width": page.width,
                        "height": page.height,
                    },
                )

        plain_text = self.exporter.document_plain_text(pages)
        markdown = self.exporter.document_markdown(pages)
        return OcrDocumentResult(
            document_id=document_id,
            workspace_id=workspace_id,
            source=source,
            engine={
                "ocr": engine.name,
                "layout": self.settings.paddleocr_structure_model,
                "formula": self.settings.paddleocr_formula_model,
                "version": engine.version,
            },
            pages=pages,
            plain_text=plain_text,
            markdown=markdown,
            created_at=utcnow().isoformat(),
        )

    def write_outputs(self, result: OcrDocumentResult, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "ocr.json"
        markdown_path = output_dir / "ocr.md"
        text_path = output_dir / "ocr.txt"
        layout_path = output_dir / "layout.json"
        formula_path = output_dir / "formulas.json"
        tables_path = output_dir / "tables.json"
        result_payload = result.to_dict()
        json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(self.exporter.export_markdown(result), encoding="utf-8")
        text_path.write_text(self.exporter.export_text(result), encoding="utf-8")
        layout_path.write_text(
            json.dumps({"pages": result_payload["pages"]}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        formula_path.write_text(
            json.dumps(
                {"blocks": _blocks_by_type(result_payload, "formula")}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        tables_path.write_text(
            json.dumps({"blocks": _blocks_by_type(result_payload, "table")}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "json": json_path,
            "markdown": markdown_path,
            "text": text_path,
            "layout": layout_path,
            "formula": formula_path,
            "tables": tables_path,
        }

    def _options(self) -> OcrOptions:
        return OcrOptions(
            engine=self.settings.ocr_engine,
            temp_dir=self.settings.ocr_temp_dir,
            max_pages=self.settings.ocr_max_pages,
            max_file_size_mb=self.settings.ocr_max_file_size_mb,
            render_dpi=self.settings.ocr_render_dpi,
            save_page_images=self.settings.ocr_save_page_images,
            enable_preprocess=self.settings.ocr_enable_preprocess,
            enable_layout=self.settings.ocr_enable_layout,
            enable_formula=self.settings.ocr_enable_formula,
            enable_table=self.settings.ocr_enable_table,
            paddleocr_use_gpu=self.settings.paddleocr_use_gpu,
            paddleocr_lang=self.settings.paddleocr_lang,
            paddleocr_det_model_dir=self.settings.paddleocr_det_model_dir,
            paddleocr_rec_model_dir=self.settings.paddleocr_rec_model_dir,
            paddleocr_cls_model_dir=self.settings.paddleocr_cls_model_dir,
            paddleocr_structure_model=self.settings.paddleocr_structure_model,
            paddleocr_formula_model=self.settings.paddleocr_formula_model,
        )


def _safe_temp_root(value: str) -> str | None:
    path = Path(value or "/tmp/ocr")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _blocks_by_type(payload: dict, block_type: str) -> list[dict]:
    return [
        {"page_index": page["page_index"], **block}
        for page in payload.get("pages", [])
        for block in page.get("blocks", [])
        if block.get("type") == block_type
    ]
