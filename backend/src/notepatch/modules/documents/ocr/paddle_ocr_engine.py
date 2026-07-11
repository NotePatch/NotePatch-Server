from __future__ import annotations

from pathlib import Path
from typing import Any

from notepatch.modules.documents.ocr.base import OcrBlock, OcrOptions, OcrPageResult
from notepatch.modules.documents.ocr.image_preprocessor import image_size
from notepatch.platform.errors import RetryableTaskError


class PaddleOcrUnavailable(RetryableTaskError):
    pass


class PaddleOcrEngine:
    name = "paddleocr"
    version = "unknown"

    def __init__(self, options: OcrOptions) -> None:
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on optional package.
            raise PaddleOcrUnavailable(f"PaddleOCR is not available: {exc}") from exc

        kwargs: dict[str, Any] = {"lang": options.paddleocr_lang}
        legacy_kwargs: dict[str, Any] = {
            **kwargs,
            "use_angle_cls": True,
            "use_gpu": options.paddleocr_use_gpu,
        }
        if options.paddleocr_det_model_dir:
            legacy_kwargs["det_model_dir"] = options.paddleocr_det_model_dir
        if options.paddleocr_rec_model_dir:
            legacy_kwargs["rec_model_dir"] = options.paddleocr_rec_model_dir
        if options.paddleocr_cls_model_dir:
            legacy_kwargs["cls_model_dir"] = options.paddleocr_cls_model_dir
        modern_kwargs = {**kwargs, "device": "gpu" if options.paddleocr_use_gpu else "cpu"}
        try:
            try:
                self._ocr = PaddleOCR(**legacy_kwargs)
            except TypeError:
                self._ocr = PaddleOCR(**modern_kwargs)
        except Exception as exc:  # pragma: no cover - depends on optional package/model.
            raise PaddleOcrUnavailable(f"PaddleOCR initialization failed: {exc}") from exc

    def recognize(self, image_path: Path, *, page_index: int, options: OcrOptions) -> OcrPageResult:
        width, height = image_size(image_path)
        raw = self._recognize_raw(image_path)
        lines = self._normalize_raw_result(raw)
        blocks: list[OcrBlock] = []
        text_lines: list[str] = []
        for order, item in enumerate(lines, start=1):
            bbox = _bbox_from_points(item["points"])
            text = item["text"]
            confidence = float(item["confidence"])
            text_lines.append(text)
            blocks.append(
                OcrBlock(
                    id=f"block_{page_index + 1:04d}_{order:04d}",
                    type="text",
                    bbox=bbox,
                    text=text,
                    confidence=confidence,
                    reading_order=order,
                    metadata={"engine": self.name},
                )
            )
        plain_text = "\n".join(text_lines)
        return OcrPageResult(
            page_index=page_index,
            width=width,
            height=height,
            rotation=0,
            blocks=blocks,
            plain_text=plain_text,
            markdown=plain_text,
        )

    def _recognize_raw(self, image_path: Path) -> Any:
        if hasattr(self._ocr, "ocr"):
            try:
                return self._ocr.ocr(str(image_path), cls=True)
            except TypeError:
                return self._ocr.ocr(str(image_path))
        if hasattr(self._ocr, "predict"):
            try:
                return self._ocr.predict(str(image_path))
            except TypeError:
                return self._ocr.predict(input=str(image_path))
        raise PaddleOcrUnavailable("PaddleOCR object does not expose ocr() or predict()")

    @staticmethod
    def _normalize_raw_result(raw: Any) -> list[dict[str, Any]]:
        if not _has_items(raw):
            return []
        if isinstance(raw, dict):
            return _normalize_dict_result(raw)
        page_items = _paddle2_page_items(raw)
        if page_items is None and _looks_like_paddle3_results(raw):
            normalized: list[dict[str, Any]] = []
            for item in raw:
                normalized.extend(_normalize_dict_result(_result_to_dict(item)))
            return normalized
        page_items = page_items if page_items is not None else []
        normalized = []
        for item in page_items:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            points = item[0]
            text_conf = item[1]
            if not isinstance(text_conf, (list, tuple)) or len(text_conf) < 2:
                continue
            normalized.append(
                {
                    "points": points,
                    "text": str(text_conf[0]),
                    "confidence": float(text_conf[1] or 0.0),
                }
            )
        return normalized


def _bbox_from_points(points: Any) -> tuple[int, int, int, int]:
    if isinstance(points, (list, tuple)) and len(points) == 4 and all(isinstance(value, (int, float)) for value in points):
        x1, y1, x2, y2 = points
        return (int(x1), int(y1), int(x2), int(y2))
    xs = []
    ys = []
    for point in _as_list(points):
        point = _to_python(point)
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    if not xs or not ys:
        return (0, 0, 0, 0)
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def _paddle2_page_items(raw: Any) -> list[Any] | None:
    if not isinstance(raw, list) or not raw:
        return None
    if _looks_like_paddle2_line(raw[0]):
        return raw
    first_page = raw[0]
    if isinstance(first_page, list) and first_page and _looks_like_paddle2_line(first_page[0]):
        return first_page
    return None


def _looks_like_paddle3_results(raw: Any) -> bool:
    return isinstance(raw, list) and bool(raw) and all(_result_to_dict(item) for item in raw)


def _looks_like_paddle2_line(item: Any) -> bool:
    return (
        isinstance(item, (list, tuple))
        and len(item) >= 2
        and isinstance(item[1], (list, tuple))
        and len(item[1]) >= 2
    )


def _result_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    for attr in ("to_dict", "dict"):
        method = getattr(item, attr, None)
        if callable(method):
            value = method()
            if isinstance(value, dict):
                return value
    json_value = getattr(item, "json", None)
    if callable(json_value):
        json_value = json_value()
    if isinstance(json_value, dict):
        return json_value
    data = getattr(item, "data", None)
    if isinstance(data, dict):
        return data
    return {}


def _normalize_dict_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "res" in payload and isinstance(payload["res"], dict):
        payload = payload["res"]
    texts = _first_nonempty(payload, "rec_texts", "texts", "text", default=[])
    if isinstance(texts, str):
        texts = [texts]
    texts = _as_list(texts)
    scores = _first_nonempty(payload, "rec_scores", "scores", "confidence", default=[])
    if isinstance(scores, (int, float)):
        scores = [scores]
    scores = _as_list(scores)
    boxes = _as_list(_first_nonempty(payload, "rec_polys", "rec_boxes", "dt_polys", "boxes", "polys"))
    normalized = []
    for index, text in enumerate(texts):
        score = scores[index] if index < len(scores) else 0.0
        points = boxes[index] if index < len(boxes) else []
        normalized.append({"points": points, "text": str(text), "confidence": float(score or 0.0)})
    return normalized


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
