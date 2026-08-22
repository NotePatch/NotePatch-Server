from __future__ import annotations

import gc
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .config import DOCTR_ROOT, ILLUMINATION_BATCH_SIZE, PNG_COMPRESS_LEVEL, REQUIRED_WEIGHT_FILES


class DocTrWeightsMissing(RuntimeError):
    pass


_MODEL_LOCK = threading.Lock()
_MODEL_STATE: dict[str, Any] | None = None


def missing_weight_paths() -> list[Path]:
    return [path for path in REQUIRED_WEIGHT_FILES.values() if not path.exists()]


def check_model_weights() -> None:
    missing = missing_weight_paths()
    if missing:
        missing_list = "\n".join(f" - {path}" for path in missing)
        raise DocTrWeightsMissing(
            "DocTr model weights are missing:\n"
            f"{missing_list}\n"
            "Download the pretrained models from the DocTr README and place them under "
            f"{DOCTR_ROOT / 'model_pretrained'}."
        )


def _ensure_doctr_import_path() -> None:
    if not DOCTR_ROOT.exists():
        raise RuntimeError(f"DocTr repository not found at {DOCTR_ROOT}")
    doctr_path = str(DOCTR_ROOT)
    if doctr_path not in sys.path:
        sys.path.insert(0, doctr_path)


def _load_state_dict(torch: Any, model: Any, path: Path) -> None:
    model_state = model.state_dict()
    loaded_state = torch.load(str(path), map_location="cuda:0")
    if isinstance(loaded_state, dict) and "state_dict" in loaded_state:
        loaded_state = loaded_state["state_dict"]

    filtered = {}
    for key, value in loaded_state.items():
        candidates = [key]
        if key.startswith("module."):
            candidates.append(key.removeprefix("module."))
        candidates.extend([key[7:], key[6:]])

        for candidate in candidates:
            if candidate in model_state and getattr(model_state[candidate], "shape", None) == getattr(value, "shape", None):
                filtered[candidate] = value
                break

    if not filtered:
        raise RuntimeError(f"No compatible parameters were loaded from {path}")

    model_state.update(filtered)
    model.load_state_dict(model_state)


def _load_models() -> dict[str, Any]:
    check_model_weights()
    _ensure_doctr_import_path()

    import cv2
    import numpy as np
    import torch
    import torch.nn as nn
    import torch.nn.functional as functional
    from GeoTr import GeoTr
    from IllTr import IllTr
    from inference_ill import composePatch, padCropImg
    from PIL import Image, ImageOps
    from seg import U2NETP

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Run this service with Docker GPU support on the RTX 4080 host.")

    class GeoTrSeg(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.msk = U2NETP(3, 1)
            self.GeoTr = GeoTr(num_attn_layers=6)

        def forward(self, x: Any) -> Any:
            mask, *_ = self.msk(x)
            mask = (mask > 0.5).float()
            x = mask * x
            bm = self.GeoTr(x)
            return (2 * (bm / 286.8) - 1) * 0.99

    geo_model = GeoTrSeg().cuda()
    _load_state_dict(torch, geo_model.msk, REQUIRED_WEIGHT_FILES["segmentation"])
    _load_state_dict(torch, geo_model.GeoTr, REQUIRED_WEIGHT_FILES["geometric"])

    ill_model = IllTr().cuda()
    _load_state_dict(torch, ill_model, REQUIRED_WEIGHT_FILES["illumination"])

    geo_model.eval()
    ill_model.eval()

    return {
        "torch": torch,
        "F": functional,
        "cv2": cv2,
        "np": np,
        "Image": Image,
        "ImageOps": ImageOps,
        "geo_model": geo_model,
        "ill_model": ill_model,
        "composePatch": composePatch,
        "padCropImg": padCropImg,
    }


def _models() -> dict[str, Any]:
    global _MODEL_STATE
    if _MODEL_STATE is None:
        with _MODEL_LOCK:
            if _MODEL_STATE is None:
                _MODEL_STATE = _load_models()
    return _MODEL_STATE


def release_models() -> None:
    """Release cached DocTr models so the shared GPU is available to OCR."""
    global _MODEL_STATE
    with _MODEL_LOCK:
        models = _MODEL_STATE
        _MODEL_STATE = None
    if models is None:
        return
    torch = models.get("torch")
    models.clear()
    del models
    gc.collect()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


def _batched_illumination_rectification(models: dict[str, Any], img_geo: Any, output_file: Path) -> None:
    torch = models["torch"]
    np = models["np"]
    Image = models["Image"]
    ill_model = models["ill_model"]
    compose_patch = models["composePatch"]
    pad_crop_img = models["padCropImg"]
    batch_size = max(1, ILLUMINATION_BATCH_SIZE)

    total_patches, pad_h, pad_w = pad_crop_img(img_geo)
    y_count, x_count = total_patches.shape[:2]
    flat_patches = total_patches.astype(np.float32).reshape(-1, 128, 128, 3) / 255.0
    results = []

    with torch.no_grad():
        for start in range(0, flat_patches.shape[0], batch_size):
            batch_np = flat_patches[start : start + batch_size]
            batch = torch.from_numpy(batch_np).permute(0, 3, 1, 2).cuda()
            output = ill_model(batch).permute(0, 2, 3, 1).detach().cpu().numpy()
            results.append(np.clip(output * 255.0, 0, 255).astype(np.uint8))

    total_results = np.concatenate(results, axis=0).reshape(y_count, x_count, 128, 128, 3)
    result_image = compose_patch(total_results, pad_h, pad_w, img_geo)
    Image.fromarray(result_image).save(output_file, compress_level=PNG_COMPRESS_LEVEL)


def rectify_document(input_path: str, output_path: str, ill_rec: bool = False) -> None:
    """Rectify one photographed document image and write the final PNG output."""
    models = _models()
    torch = models["torch"]
    functional = models["F"]
    cv2 = models["cv2"]
    np = models["np"]
    Image = models["Image"]
    ImageOps = models["ImageOps"]
    geo_model = models["geo_model"]

    input_file = Path(input_path)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    image = ImageOps.exif_transpose(Image.open(input_file)).convert("RGB")
    im_ori = np.array(image, dtype=np.float32) / 255.0
    h, w, _ = im_ori.shape
    im = cv2.resize(im_ori, (288, 288)).transpose(2, 0, 1)
    im = torch.from_numpy(im).float().unsqueeze(0).cuda()

    with torch.no_grad():
        bm = geo_model(im).cpu()
        bm0 = cv2.resize(bm[0, 0].numpy(), (w, h))
        bm1 = cv2.resize(bm[0, 1].numpy(), (w, h))
        bm0 = cv2.blur(bm0, (3, 3))
        bm1 = cv2.blur(bm1, (3, 3))
        grid = torch.from_numpy(np.stack([bm0, bm1], axis=2)).unsqueeze(0)
        source = torch.from_numpy(im_ori).permute(2, 0, 1).unsqueeze(0).float()
        out = functional.grid_sample(source, grid, align_corners=True)

    img_geo = np.clip((out[0] * 255).permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
    tmp_output = output_file.with_name(f".{output_file.name}.tmp.png")
    try:
        if ill_rec:
            _batched_illumination_rectification(models, img_geo, tmp_output)
        else:
            Image.fromarray(img_geo).save(tmp_output, compress_level=PNG_COMPRESS_LEVEL)
        os.replace(tmp_output, output_file)
    finally:
        if tmp_output.exists():
            tmp_output.unlink()
