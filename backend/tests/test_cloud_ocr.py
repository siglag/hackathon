import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from app import pipeline
from app.agent import openrouter
from app.ingest import ocr
from app.jobs import Cancelled, JobError


@pytest.fixture
def cloud(monkeypatch):
    config = replace(ocr.settings, ocr_engine="openrouter", allow_cloud_model=True,
                     ocr_model="google/gemini-3.8-flash")
    monkeypatch.setattr(ocr, "settings", config)
    monkeypatch.setattr(pipeline, "settings", config)
    image = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(image, format="PNG")
    return image.getvalue()


def test_gemini_boxes_match_image_review_coordinates(monkeypatch, cloud):
    requests = []

    def respond(body):
        requests.append(body)
        return {"choices": [{"message": {"content": json.dumps({"spans": [
            {"text": "5,8 mg/dL", "box": [100, 200, 300, 600]},
        ]})}}]}

    monkeypatch.setattr(openrouter, "_post", respond)
    spans = ocr.recognize_png(cloud)
    assert spans == [{"text": "5,8 mg/dL", "bbox": [80.0, 20.0, 240.0, 60.0],
                      "score": None, "source": "ocr"}]
    assert requests[0]["model"] == "google/gemini-3.8-flash"
    assert requests[0]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("reply", [{}, {"choices": []}, {"choices": [{"message": {"content": '{"spans":null}'}}]}])
def test_malformed_cloud_reply_has_actionable_error(monkeypatch, cloud, reply):
    monkeypatch.setattr(openrouter, "_post", lambda body: reply)
    with pytest.raises(ocr.OCRUnavailable, match="unreadable reply"):
        ocr.recognize_png(cloud)


def test_cloud_ocr_requires_opt_in_before_sending(monkeypatch, cloud):
    monkeypatch.setattr(ocr, "settings", replace(ocr.settings, allow_cloud_model=False))
    monkeypatch.setattr(openrouter, "_post", lambda body: pytest.fail("Image must not be sent"))
    with pytest.raises(ocr.OCRUnavailable, match="disabled"):
        ocr.recognize_png(cloud)


@pytest.mark.parametrize("error, expected", [(RuntimeError("provider failure"), "Cloud OCR failed"),
                                            (MemoryError(), "ran out of memory")])
def test_pipeline_reports_correct_ocr_recovery(monkeypatch, cloud, error, expected):
    def fail(png):
        raise error

    monkeypatch.setattr(ocr, "recognize_png", fail)
    ctx = SimpleNamespace(id="synthetic-job", stage=lambda *a, **kw: None)
    with pytest.raises(JobError, match=expected) as raised:
        pipeline._ocr_page(ctx, cloud, 1, 1)
    assert "setup_models.py" not in str(raised.value)


def test_ocr_cancellation_remains_cancellation(monkeypatch, cloud):
    def cancel(png):
        raise Cancelled()

    monkeypatch.setattr(ocr, "recognize_png", cancel)
    ctx = SimpleNamespace(id="synthetic-job", stage=lambda *a, **kw: None)
    with pytest.raises(Cancelled):
        pipeline._ocr_page(ctx, cloud, 1, 1)
