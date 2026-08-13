from __future__ import annotations

from pathlib import Path
from typing import Any
from importlib.metadata import PackageNotFoundError, version

from notepatch.platform.errors import RetryableTaskError
from notepatch.modules.documents.ocr.base import OcrBlock, OcrOptions, OcrPageResult
from notepatch.modules.documents.ocr.image_preprocessor import image_size


class PaddleStructureUnavailable(RetryableTaskError):
    pass


class PaddleStructureEngine:
    name = "pp-ocrv5"
    version = "3.7.0"

    def __init__(self, options: OcrOptions) -> None:
        if options.paddleocr_use_gpu:
            _assert_cuda_runtime()
        try:
            from paddleocr import PPStructureV3
        except Exception as exc:
            raise PaddleStructureUnavailable(f"PP-StructureV3 is not available: {exc}") from exc

        kwargs: dict[str, Any] = {
            "device": "gpu:0" if options.paddleocr_use_gpu else "cpu",
            "lang": options.paddleocr_lang,
            "use_formula_recognition": options.enable_formula,
            "use_table_recognition": options.enable_table,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": True,
        }
        if options.paddleocr_formula_model:
            kwargs["formula_recognition_model_name"] = options.paddleocr_formula_model
        if options.paddleocr_det_model_dir:
            kwargs["text_detection_model_dir"] = options.paddleocr_det_model_dir
        if options.paddleocr_rec_model_dir:
            kwargs["text_recognition_model_dir"] = options.paddleocr_rec_model_dir
        if options.paddleocr_cls_model_dir:
            kwargs["textline_orientation_model_dir"] = options.paddleocr_cls_model_dir
        try:
            self.pipeline = PPStructureV3(**kwargs)
        except TypeError:
            kwargs.pop("formula_recognition_model_name", None)
            try:
                self.pipeline = PPStructureV3(**kwargs)
            except Exception as exc:
                raise PaddleStructureUnavailable(f"PP-StructureV3 initialization failed: {exc}") from exc
        except Exception as exc:
            raise PaddleStructureUnavailable(f"PP-StructureV3 initialization failed: {exc}") from exc
        try:
            self.version = version("paddleocr")
        except PackageNotFoundError:
            pass

    def recognize(self, image_path: Path, *, page_index: int, options: OcrOptions) -> OcrPageResult:
        width, height = image_size(image_path)
        try:
            raw_results = list(self.pipeline.predict(str(image_path)))
        except Exception as exc:
            raise PaddleStructureUnavailable(f"PP-StructureV3 inference failed: {exc}") from exc
        if not raw_results:
            return OcrPageResult(page_index=page_index, width=width, height=height, blocks=[])
        payload = _result_to_dict(raw_results[0])
        blocks = _normalize_structure_blocks(payload, page_index)
        return OcrPageResult(page_index=page_index, width=width, height=height, blocks=blocks)



def _assert_cuda_runtime() -> None:
    """Fail before model loading when a stale container silently falls back to CPU."""
    try:
        import paddle

        if not paddle.device.is_compiled_with_cuda():
            raise RuntimeError("PaddlePaddle was not compiled with CUDA support")
        if paddle.device.cuda.device_count() < 1:
            raise RuntimeError("No CUDA device is visible")
        paddle.set_device("gpu:0")
        probe = paddle.ones([1], dtype="float32")
        probe.numpy()
        selected_device = paddle.device.get_device()
        if not selected_device.startswith("gpu"):
            raise RuntimeError(f"Paddle selected {selected_device} instead of gpu:0")
    except Exception as exc:
        raise PaddleStructureUnavailable(f"Paddle CUDA runtime is unavailable: {exc}") from exc


def _result_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    for attr in ("json", "to_dict", "dict"):
        value = getattr(item, attr, None)
        if callable(value):
            value = value()
        if isinstance(value, dict):
            return value
    data = getattr(item, "data", None)
    return data if isinstance(data, dict) else {}


def _normalize_structure_blocks(payload: dict[str, Any], page_index: int) -> list[OcrBlock]:
    root = payload.get("res") if isinstance(payload.get("res"), dict) else payload
    parsing = _as_list(_first_nonempty(root, "parsing_res_list", "layout_parsing_result"))
    blocks: list[OcrBlock] = []
    for order, raw in enumerate(parsing, start=1):
        item = _result_to_dict(raw)
        label = str(_first_nonempty(item, "block_label", "label", "type", default="text")).lower()
        content = str(_first_nonempty(item, "block_content", "content", "text", default="")).strip()
        bbox = _bbox(_first_nonempty(item, "block_bbox", "bbox", "coordinate"))
        confidence = float(_first_nonempty(item, "block_score", "score", "confidence", default=0.0))
        block_type = _block_type(label)
        html = _first_nonempty(item, "html", "pred_html")
        markdown = _first_nonempty(item, "markdown", "table_markdown")
        blocks.append(
            OcrBlock(
                id=f"block_{page_index + 1:04d}_{order:04d}",
                type=block_type,
                bbox=bbox,
                text=content if block_type == "text" else None,
                latex=content if block_type == "formula" else None,
                html=str(html) if _has_items(html) else None,
                markdown=str(markdown if _has_items(markdown) else content) if block_type == "table" else None,
                confidence=confidence,
                reading_order=order,
                metadata={"engine": "pp-structure-v3", "label": label},
            )
        )
    if blocks:
        return blocks

    ocr = root.get("overall_ocr_res") if isinstance(root.get("overall_ocr_res"), dict) else root
    texts = _as_list(ocr.get("rec_texts"))
    boxes = _as_list(_first_nonempty(ocr, "rec_boxes", "rec_polys"))
    scores = _as_list(ocr.get("rec_scores"))
    for order, text in enumerate(texts, start=1):
        blocks.append(
            OcrBlock(
                id=f"block_{page_index + 1:04d}_{order:04d}",
                type="text",
                bbox=_bbox(boxes[order - 1] if order <= len(boxes) else None),
                text=str(text),
                confidence=float(scores[order - 1] if order <= len(scores) else 0.0),
                reading_order=order,
                metadata={"engine": "pp-structure-v3", "label": "text"},
            )
        )
    return blocks


def _block_type(label: str) -> str:
    if "formula" in label or "equation" in label:
        return "formula"
    if "table" in label:
        return "table"
    return "text"


def _bbox(value: Any) -> tuple[int, int, int, int]:
    value = _to_python(value)
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(x, (int, float)) for x in value):
        return tuple(int(x) for x in value)  # type: ignore[return-value]
    points = value if isinstance(value, (list, tuple)) else []
    normalized_points = [_to_python(point) for point in points]
    xs = [float(point[0]) for point in normalized_points if isinstance(point, (list, tuple)) and len(point) >= 2]
    ys = [float(point[1]) for point in normalized_points if isinstance(point, (list, tuple)) and len(point) >= 2]
    if not xs or not ys:
        return (0, 0, 0, 0)
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def _has_items(value: Any) -> bool:
    if value is None:
        return False
    try:
        return len(value) > 0
    except TypeError:
        return True


def _first_nonempty(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if _has_items(value):
            return value
    return default


def _to_python(value: Any) -> Any:
    tolist = getattr(value, "tolist", None)
    return tolist() if callable(tolist) else value


def _as_list(value: Any) -> list[Any]:
    value = _to_python(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]
