from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


BBox = tuple[int, int, int, int]


@dataclass
class OcrOptions:
    engine: str = "paddleocr"
    temp_dir: str = "/tmp/ocr"
    max_pages: int = 50
    max_file_size_mb: int = 200
    render_dpi: int = 200
    save_page_images: bool = False
    enable_preprocess: bool = True
    enable_layout: bool = True
    enable_formula: bool = True
    enable_table: bool = True
    paddleocr_use_gpu: bool = True
    paddleocr_lang: str = "ch"
    paddleocr_det_model_dir: str | None = None
    paddleocr_rec_model_dir: str | None = None
    paddleocr_cls_model_dir: str | None = None
    paddleocr_structure_model: str = "PP-StructureV3"
    paddleocr_formula_model: str = "PP-FormulaNet_plus-M"


@dataclass
class OcrBlock:
    id: str
    type: str
    bbox: BBox
    confidence: float
    reading_order: int
    text: str | None = None
    latex: str | None = None
    html: str | None = None
    markdown: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "bbox": list(self.bbox),
            "confidence": self.confidence,
            "reading_order": self.reading_order,
        }
        for key in ("text", "latex", "html", "markdown"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass
class OcrPageResult:
    page_index: int
    width: int
    height: int
    rotation: int = 0
    blocks: list[OcrBlock] = field(default_factory=list)
    plain_text: str = ""
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "blocks": [block.to_dict() for block in self.blocks],
            "plain_text": self.plain_text,
            "markdown": self.markdown,
        }


@dataclass
class OcrDocumentResult:
    document_id: str
    workspace_id: str
    source: dict[str, Any]
    engine: dict[str, Any]
    pages: list[OcrPageResult]
    plain_text: str
    markdown: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "workspace_id": self.workspace_id,
            "source": self.source,
            "engine": self.engine,
            "pages": [page.to_dict() for page in self.pages],
            "plain_text": self.plain_text,
            "markdown": self.markdown,
            "created_at": self.created_at,
        }


class OcrEngine(Protocol):
    name: str
    version: str

    def recognize(self, image_path: Path, *, page_index: int, options: OcrOptions) -> OcrPageResult:
        ...


class LayoutEngine(Protocol):
    name: str
    version: str


class FormulaEngine(Protocol):
    name: str
    version: str

    def apply(self, page: OcrPageResult, *, image_path: Path, options: OcrOptions) -> OcrPageResult:
        ...
