"""OCR (spec section 8): local PaddleOCR, or a cloud vision model via OpenRouter.

``paddle`` (spec default): models are downloaded during setup
(``scripts/setup_models.py``) with no patient content loaded. Recognition
scores are engine scores, not calibrated probabilities of clinical correctness.

``openrouter`` (EXPERIMENTAL, branch ``deepseek-openrouter``): the page image,
including any names or identifiers printed on it, is sent to OpenRouter and the
upstream provider. Requires MEDASSIST_ALLOW_CLOUD_MODEL=1. Use synthetic data.
The model reports no confidence, so spans carry ``score: None``.
"""
from __future__ import annotations

import base64
import logging
import os
import struct
import tempfile
import threading
from pathlib import Path

from ..config import settings

log = logging.getLogger("medassist.ocr")

ENGINE_VERSION = f"openrouter:{settings.ocr_model}" if settings.ocr_engine == "openrouter" else "paddleocr-PP-OCRv5"
# Runtime must not contact model hosters; models come from the setup step's cache.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
_engine = None
_lock = threading.Lock()


class OCRUnavailable(RuntimeError):
    pass


def _get_engine():
    global _engine
    if settings.ocr_engine == "none":
        raise OCRUnavailable("OCR is disabled in configuration. Use manual transcription.")
    with _lock:
        if _engine is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as e:
                raise OCRUnavailable("PaddleOCR is not installed (pip install '.[ocr]').") from e
            _engine = PaddleOCR(
                lang=settings.ocr_lang,
                ocr_version="PP-OCRv5",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,  # OpenCV handles perspective/deskew
                use_textline_orientation=False,
                # Detect on a bounded image (recognition still crops from the full page) to cap memory.
                text_det_limit_side_len=settings.ocr_det_side,
                text_det_limit_type="max",
                enable_mkldnn=False,  # Paddle 3.3 oneDNN kernels fail on these models
            )
        return _engine


def available() -> tuple[bool, str]:
    if settings.ocr_engine == "none":
        return False, "disabled"
    if settings.ocr_engine == "openrouter":
        if not settings.allow_cloud_model:
            return False, "cloud OCR not allowed (MEDASSIST_ALLOW_CLOUD_MODEL=0)"
        if not settings.openrouter_api_key():
            return False, "OpenRouter API key not configured"
        return True, ENGINE_VERSION
    try:
        import paddleocr  # noqa: F401
    except ImportError:
        return False, "paddleocr not installed"
    return True, ENGINE_VERSION


def recognize_png(png: bytes) -> list[dict]:
    """Return spans: {text, bbox[x0,y0,x1,y1], score, source:'ocr'}."""
    if settings.ocr_engine == "openrouter":
        return _recognize_openrouter(png)
    engine = _get_engine()
    with tempfile.TemporaryDirectory(prefix="ocr-") as d:
        p = Path(d) / "page.png"
        p.write_bytes(png)
        with _lock:
            results = engine.predict(str(p))
    spans: list[dict] = []
    for r in results:
        j = r.json if hasattr(r, "json") else r
        j = j.get("res", j)
        for text, score, box in zip(j.get("rec_texts", []), j.get("rec_scores", []), j.get("rec_boxes", [])):
            if not str(text).strip():
                continue
            spans.append({
                "text": str(text),
                "bbox": [float(v) for v in list(box)[:4]],
                "score": round(float(score), 4),
                "source": "ocr",
            })
    return spans


_PROMPT = (
    "You are an OCR engine. Transcribe every piece of printed or handwritten text in this document image exactly as "
    "written: keep spelling, punctuation, decimal commas, units, flags (H, L, *) and comparators (<, >). Never correct, "
    "translate, normalize or invent text, and skip nothing.\n"
    "Return one entry per text segment. In tables, every cell is its own entry (test name, value, unit, reference "
    "range, flag); outside tables, one entry per contiguous phrase on a line.\n"
    "Give each entry a tight bounding box {box} in coordinates normalized to 0-1000 relative to the image "
    "width and height (origin top-left).\n"
    'Reply with JSON only: {{"spans": [{{"text": "...", "box": {box}}}]}}'
)


def _yx_boxes() -> bool:
    # Gemini is trained on [ymin, xmin, ymax, xmax] and returns that order even when asked otherwise.
    return settings.ocr_model.startswith("google/")


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])


def _recognize_openrouter(png: bytes) -> list[dict]:
    from ..agent import openrouter
    from ..agent.ollama import ModelUnavailable
    from .extraction import parse_json_reply

    if not settings.allow_cloud_model:
        raise OCRUnavailable("Cloud OCR is disabled: set MEDASSIST_ALLOW_CLOUD_MODEL=1 to send page images to OpenRouter.")
    w, h = _png_size(png)
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    try:
        data = openrouter._post({
            "model": settings.ocr_model, "temperature": 0, "max_tokens": settings.ocr_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": _PROMPT.format(box="[ymin, xmin, ymax, xmax]" if _yx_boxes() else "[x0, y0, x1, y1]")},
            ]}],
        })
    except ModelUnavailable as e:
        raise OCRUnavailable(f"Cloud OCR failed: {e}.") from None
    try:
        items = parse_json_reply(data["choices"][0]["message"].get("content") or "").get("spans", [])
        if not isinstance(items, list):
            raise ValueError("spans must be a list")
    except (ValueError, AttributeError, KeyError, IndexError, TypeError):
        raise OCRUnavailable("Cloud OCR returned an unreadable reply; retry or transcribe manually.") from None
    spans: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # Gemini sometimes names the text field "label" (its detection format).
        text, box = str(it.get("text") or it.get("label") or "").strip(), it.get("box") or it.get("bbox")
        if not text or not isinstance(box, list) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = (min(max(float(v), 0.0), 1000.0) for v in box)
        except (TypeError, ValueError):
            continue
        if _yx_boxes():
            x0, y0, x1, y1 = y0, x0, y1, x1
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        spans.append({
            "text": text,
            "bbox": [round(x0 * w / 1000, 1), round(y0 * h / 1000, 1), round(x1 * w / 1000, 1), round(y1 * h / 1000, 1)],
            "score": None,  # vision models report no recognition confidence
            "source": "ocr",
        })
    return spans
