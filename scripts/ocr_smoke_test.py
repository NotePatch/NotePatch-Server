from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notepatch.platform.config import get_settings
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.shared.filenames import infer_file_type


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OCR pipeline against a local image or PDF.")
    parser.add_argument("input", help="Local image or PDF path.")
    parser.add_argument("--output-dir", default="/tmp/notepatch-ocr-smoke", help="Directory for ocr.json/md/txt.")
    parser.add_argument("--mime-type", default=None, help="Override input MIME type.")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file does not exist: {input_path}")

    settings = get_settings()
    settings.ocr_engine = "paddleocr"

    mime_type = args.mime_type or _guess_mime_type(input_path)
    file_type = infer_file_type(input_path.name, mime_type)
    result = OcrPipeline(settings=settings).run(
        input_path=input_path,
        document_id="local-smoke",
        workspace_id="local-smoke",
        source={
            "filename": input_path.name,
            "mime_type": mime_type,
            "file_size": input_path.stat().st_size,
        },
        mime_type=mime_type,
        file_type=file_type,
        event_callback=lambda event, message, data: print(f"{event}: {message} {data}"),
    )
    paths = OcrPipeline(settings=settings).write_outputs(result, Path(args.output_dir).expanduser().resolve())
    print("OCR outputs:")
    for kind, path in paths.items():
        print(f"{kind}: {path}")


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    return "image/png"


if __name__ == "__main__":
    main()
