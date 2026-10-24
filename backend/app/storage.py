"""Local file storage. Files are addressed by opaque storage refs, never by the
uploaded filename, and are written owner-read-only.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import settings
from .util import new_id


def _case_dir(case_id: str) -> Path:
    if not case_id.replace("_", "").isalnum():
        raise ValueError("bad case id")
    d = settings.files_dir / case_id
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def put(case_id: str, data: bytes, suffix: str = "", kind: str = "f") -> str:
    ref = f"{case_id}/{new_id(kind)}{suffix}"
    path = settings.files_dir / ref
    _case_dir(case_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    try:
        path.chmod(0o400 if kind == "orig" else 0o600)
    except OSError:
        pass
    return ref


def path_of(ref: str) -> Path:
    p = (settings.files_dir / ref).resolve()
    root = settings.files_dir.resolve()
    if root not in p.parents:
        raise ValueError("storage ref escapes data directory")
    return p


def read(ref: str) -> bytes:
    return path_of(ref).read_bytes()


def delete_case(case_id: str) -> None:
    d = settings.files_dir / case_id
    if d.exists():
        for p in d.rglob("*"):
            try:
                p.chmod(0o600)
            except OSError:
                pass
        shutil.rmtree(d)


def cache_dir(case_id: str) -> Path:
    d = _case_dir(case_id) / "cache"
    d.mkdir(exist_ok=True, mode=0o700)
    return d
