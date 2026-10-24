"""Local OCR with PaddleOCR (spec section 8). Never uses a hosted OCR API.

Models must be downloaded during setup (``scripts/setup_models.py``) with no
patient content loaded. Recognition scores are engine scores, not calibrated
probabilities of clinical correctness.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from ..config import settings

log = logging.getLogger("medassist.ocr")

ENGINE_VERSION = "paddleocr-PP-OCRv5"
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
                enable_mkldnn=False,  # Paddle 3.3 oneDNN kernels fail on these models
                text_det_limit_type="max",
                text_det_limit_side_len=settings.ocr_det_max_side,
            )
        return _engine


def available() -> tuple[bool, str]:
    if settings.ocr_engine == "none":
        return False, "disabled"
    try:
        import paddleocr  # noqa: F401
    except ImportError:
        return False, "paddleocr not installed"
    return True, ENGINE_VERSION


def recognize_png(png: bytes) -> list[dict]:
    """Return spans: {text, bbox[x0,y0,x1,y1], score, source:'ocr'}."""
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
