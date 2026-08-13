from pathlib import Path

from PIL import Image

from notepatch.modules.documents.ocr.base import OcrBlock, OcrPageResult
from notepatch.modules.documents.ocr.ocr_pipeline import OcrPipeline
from notepatch.platform.config import get_settings


class CountingEngine:
    name = "counting-ocr"
    version = "1"

    def recognize(self, image_path: Path, *, page_index: int, options) -> OcrPageResult:
        return OcrPageResult(
            page_index=page_index,
            width=32,
            height=32,
            blocks=[
                OcrBlock(
                    id="temporary",
                    type="text",
                    bbox=(0, 0, 16, 16),
                    confidence=1.0,
                    reading_order=1,
                    text="cached engine",
                )
            ],
        )


def test_pipeline_constructs_real_engine_once_per_instance(monkeypatch, tmp_path):
    image = tmp_path / "source.png"
    Image.new("RGB", (32, 32), "white").save(image)
    constructed = []

    def factory(options):
        constructed.append(options.engine)
        return CountingEngine()

    monkeypatch.setattr("notepatch.modules.documents.ocr.ocr_pipeline.PaddleStructureEngine", factory)
    settings = get_settings()
    old_engine = settings.ocr_engine
    old_temp = settings.ocr_temp_dir
    settings.ocr_engine = "paddleocr"
    settings.ocr_temp_dir = str(tmp_path / "ocr")
    try:
        pipeline = OcrPipeline(settings=settings)
        for document_id in ("doc-1", "doc-2"):
            result = pipeline.run(
                input_path=image,
                document_id=document_id,
                workspace_id="workspace-1",
                source={"filename": image.name, "mime_type": "image/png", "file_size": image.stat().st_size},
                mime_type="image/png",
                file_type="image",
            )
            assert result.plain_text == "cached engine"
        assert constructed == ["paddleocr"]
        assert isinstance(pipeline.engine, CountingEngine)
    finally:
        settings.ocr_engine = old_engine
        settings.ocr_temp_dir = old_temp



def test_worker_constructs_pipeline_only_for_ocr_queue(monkeypatch):
    import notepatch.entrypoints.worker as worker

    pipelines = [object()]
    monkeypatch.setattr(worker, "OcrPipeline", lambda: pipelines[0])
    assert worker.ocr_pipeline_for_worker_queues(["default", "chat"]) is None
    assert worker.ocr_pipeline_for_worker_queues(["ocr"]) is pipelines[0]
