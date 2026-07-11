import builtins

from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.documents.ocr.base import OcrOptions
from notepatch.modules.documents.ocr.paddle_ocr_engine import PaddleOcrEngine
from tests.fakes import FakeOcrEngine
from tests.test_doctr_worker import PNG_BYTES


class LegacyPaddleOcr:
    def ocr(self, path: str, cls: bool = True):
        return [[[[[1, 2], [9, 2], [9, 8], [1, 8]], ("legacy text", 0.91)]]]


class ModernPaddleOcr:
    def predict(self, path: str):
        return [
            {
                "rec_texts": ["modern text"],
                "rec_scores": [0.88],
                "rec_boxes": [[1, 2, 9, 8]],
            }
        ]


def _engine(fake_ocr) -> PaddleOcrEngine:
    engine = PaddleOcrEngine.__new__(PaddleOcrEngine)
    engine._ocr = fake_ocr
    return engine


def test_injected_test_engine_does_not_import_paddleocr(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "paddleocr":
            raise AssertionError("PaddleOCR should not be imported for an explicitly injected test engine")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    input_path = tmp_path / "input.png"
    input_path.write_bytes(PNG_BYTES)

    pipeline = OcrPipeline(engine=FakeOcrEngine())
    settings = pipeline.settings
    old_temp_dir = settings.ocr_temp_dir
    settings.ocr_temp_dir = str(tmp_path / "ocr")
    try:
        result = pipeline.run(
            input_path=input_path,
            document_id="doc-1",
            workspace_id="workspace-1",
            source={"filename": "input.png", "mime_type": "image/png", "file_size": len(PNG_BYTES)},
            mime_type="image/png",
            file_type="image",
        )
    finally:
        settings.ocr_temp_dir = old_temp_dir

    assert result.engine["ocr"] == "test-ocr"


def test_paddleocr_adapter_normalizes_legacy_ocr_output(tmp_path):
    image_path = tmp_path / "legacy.png"
    image_path.write_bytes(PNG_BYTES)

    page = _engine(LegacyPaddleOcr()).recognize(image_path, page_index=0, options=OcrOptions(engine="paddleocr"))

    assert len(page.blocks) == 1
    assert page.blocks[0].text == "legacy text"
    assert page.blocks[0].bbox == (1, 2, 9, 8)
    assert page.blocks[0].confidence == 0.91


def test_paddleocr_adapter_normalizes_predict_output(tmp_path):
    image_path = tmp_path / "modern.png"
    image_path.write_bytes(PNG_BYTES)

    page = _engine(ModernPaddleOcr()).recognize(image_path, page_index=0, options=OcrOptions(engine="paddleocr"))

    assert len(page.blocks) == 1
    assert page.blocks[0].text == "modern text"
    assert page.blocks[0].bbox == (1, 2, 9, 8)
    assert page.blocks[0].confidence == 0.88
