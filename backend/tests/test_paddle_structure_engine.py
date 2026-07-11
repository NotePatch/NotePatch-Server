from pathlib import Path

from notepatch.modules.documents.ocr.base import OcrOptions
from notepatch.modules.documents.ocr.paddle_structure_engine import PaddleStructureEngine
from tests.test_doctr_worker import PNG_BYTES


class AmbiguousArray:
    def __init__(self, values):
        self.values = values

    def __bool__(self):
        raise ValueError("array truth value is ambiguous")

    def __len__(self):
        return len(self.values)

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, index):
        return self.values[index]

    def tolist(self):
        return self.values


class FakeStructurePipeline:
    def __init__(self, payload):
        self.payload = payload

    def predict(self, _path: str):
        return [self.payload]


def _engine(payload) -> PaddleStructureEngine:
    engine = PaddleStructureEngine.__new__(PaddleStructureEngine)
    engine.pipeline = FakeStructurePipeline(payload)
    return engine


def _image(tmp_path: Path) -> Path:
    path = tmp_path / "page.png"
    path.write_bytes(PNG_BYTES)
    return path


def test_empty_numpy_like_results_are_a_valid_blank_page(tmp_path):
    page = _engine(
        {
            "res": {
                "parsing_res_list": AmbiguousArray([]),
                "overall_ocr_res": {
                    "rec_texts": AmbiguousArray([]),
                    "rec_boxes": AmbiguousArray([]),
                    "rec_scores": AmbiguousArray([]),
                },
            }
        }
    ).recognize(_image(tmp_path), page_index=44, options=OcrOptions(engine="paddleocr"))

    assert page.page_index == 44
    assert page.blocks == []


def test_numpy_like_ocr_arrays_are_normalized_without_boolean_coercion(tmp_path):
    page = _engine(
        {
            "res": {
                "parsing_res_list": AmbiguousArray([]),
                "overall_ocr_res": {
                    "rec_texts": AmbiguousArray(["recognized text"]),
                    "rec_boxes": AmbiguousArray([[1, 2, 9, 8]]),
                    "rec_scores": AmbiguousArray([0.93]),
                },
            }
        }
    ).recognize(_image(tmp_path), page_index=0, options=OcrOptions(engine="paddleocr"))

    assert len(page.blocks) == 1
    assert page.blocks[0].text == "recognized text"
    assert page.blocks[0].bbox == (1, 2, 9, 8)
    assert page.blocks[0].confidence == 0.93
