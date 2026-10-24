#!/usr/bin/env python3
"""Setup/update step: download and validate local OCR models.

Run with NO patient content loaded (spec section 4). Afterwards OCR runs locally; DeepSeek inference uses OpenRouter.

    backend/.venv/bin/python scripts/setup_models.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from PIL import Image, ImageDraw  # noqa: E402

from app.ingest import ocr  # noqa: E402


def main() -> None:
    ok, eng = ocr.available()
    if not ok:
        print("OCR not installed:", eng)
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "synthetic.png"
        im = Image.new("RGB", (800, 200), "white")
        ImageDraw.Draw(im).text((20, 80), "Synthetic setup check 12.5 mg/dL", fill="black")
        im.save(p)
        spans = ocr.recognize_png(p.read_bytes())
    print(f"OCR ready ({eng}); synthetic check recognized {len(spans)} span(s).")


if __name__ == "__main__":
    main()
