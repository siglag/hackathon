"""Image normalization and quality checks with OpenCV (spec sections 7, 8).

The working copy is re-encoded from pixels only, which strips EXIF/GPS and
other metadata. The original bytes are never modified.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps

from ..config import settings

Image.MAX_IMAGE_PIXELS = settings.max_image_pixels


@dataclass
class Normalized:
    png: bytes
    width: int
    height: int
    quality: dict = field(default_factory=dict)
    operations: list[str] = field(default_factory=list)


def load_rgb(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)  # apply camera rotation before metadata is dropped
        im = im.convert("RGB")
        return np.array(im)[:, :, ::-1].copy()  # BGR for OpenCV


def strip_metadata(data: bytes) -> bytes:
    """Pixel-only PNG re-encode of an image (no EXIF, GPS, ICC text chunks)."""
    bgr = load_rgb(data)
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("encode failed")
    return buf.tobytes()


def quality_checks(gray: np.ndarray) -> dict:
    h, w = gray.shape[:2]
    small = cv2.resize(gray, (min(w, 1000), int(h * min(w, 1000) / w))) if w > 1000 else gray
    blur = float(cv2.Laplacian(small, cv2.CV_64F).var())
    glare = float((small >= 250).mean())
    warnings = []
    if blur < 60:
        warnings.append("Image appears blurry; numbers may be misread. Consider retaking.")
    if glare > 0.08:
        warnings.append("Strong glare detected; part of the page may be unreadable.")
    if min(h, w) < 700:
        warnings.append("Low resolution; move closer or use a higher-resolution capture.")
    return {"blur_score": round(blur, 1), "glare_fraction": round(glare, 3), "width": w, "height": h, "warnings": warnings}


def _perspective(bgr: np.ndarray) -> np.ndarray | None:
    """Find a dominant page quadrilateral and warp it to a rectangle."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    scale = 800 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1 else gray
    scale = min(scale, 1.0)
    edges = cv2.Canny(cv2.GaussianBlur(small, (5, 5), 0), 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 0.35 * small.shape[0] * small.shape[1] or area > 0.98 * small.shape[0] * small.shape[1]:
        return None
    approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
    if len(approx) != 4:
        return None
    pts = (approx.reshape(4, 2) / scale).astype(np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    tl, br, tr, bl = pts[np.argmin(s)], pts[np.argmax(s)], pts[np.argmin(d)], pts[np.argmax(d)]
    wd = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    ht = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if wd < 300 or ht < 300:
        return None
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl]), np.array([[0, 0], [wd, 0], [wd, ht], [0, ht]], dtype=np.float32))
    return cv2.warpPerspective(bgr, m, (wd, ht))


def _deskew_angle(gray: np.ndarray) -> float:
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    lines = cv2.HoughLinesP(thr, 1, np.pi / 720, threshold=200, minLineLength=gray.shape[1] // 4, maxLineGap=20)
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines[:, 0]:
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) < 15:
            angles.append(a)
    return float(np.median(angles)) if angles else 0.0


def normalize(data: bytes, rotate_deg: int = 0) -> Normalized:
    bgr = load_rgb(data)
    ops = ["exif_orientation_applied", "metadata_stripped"]
    if rotate_deg % 360:
        k = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate_deg % 360]
        bgr = cv2.rotate(bgr, k)
        ops.append(f"rotated_{rotate_deg % 360}")
    warped = _perspective(bgr)
    if warped is not None:
        bgr = warped
        ops.append("perspective_corrected")
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    angle = _deskew_angle(gray)
    if 0.3 < abs(angle) < 15:
        h, w = gray.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        bgr = cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        ops.append(f"deskewed_{angle:.1f}deg")
    q = quality_checks(gray)
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("encode failed")
    return Normalized(buf.tobytes(), bgr.shape[1], bgr.shape[0], q, ops)
