from pathlib import Path

from notepatch.modules.documents.ocr.base import OcrOptions
from notepatch.modules.documents.ocr.paddle_structure_engine import PaddleStructureEngine, _assert_cuda_runtime


READY_FILE = Path("/tmp/notepatch-ocr-worker-ready")


def main() -> None:
    import cv2
    import paddle

    _assert_cuda_runtime()
    if not cv2.__version__:
        raise RuntimeError("OpenCV is unavailable")
    PaddleStructureEngine(OcrOptions(paddleocr_use_gpu=True))
    READY_FILE.write_text("ready\n", encoding="ascii")


if __name__ == "__main__":
    main()
