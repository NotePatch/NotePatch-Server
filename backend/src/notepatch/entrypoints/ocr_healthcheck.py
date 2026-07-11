from pathlib import Path

from notepatch.modules.documents.ocr.base import OcrOptions
from notepatch.modules.documents.ocr.paddle_structure_engine import PaddleStructureEngine


READY_FILE = Path("/tmp/notepatch-ocr-worker-ready")


def main() -> None:
    import cv2
    import paddle

    if not paddle.device.is_compiled_with_cuda():
        raise RuntimeError("PaddlePaddle was not compiled with CUDA support")
    if paddle.device.cuda.device_count() < 1:
        raise RuntimeError("No CUDA device is visible to the OCR worker")
    if not cv2.__version__:
        raise RuntimeError("OpenCV is unavailable")
    PaddleStructureEngine(OcrOptions(paddleocr_use_gpu=True))
    READY_FILE.write_text("ready\n", encoding="ascii")


if __name__ == "__main__":
    main()
