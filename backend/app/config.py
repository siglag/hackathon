"""Runtime configuration, read from MEDASSIST_* environment variables.

Every limit in spec section 15 is configurable here. Secrets are not stored in
this module; the gateway shared secret is generated into the protected data
directory on first run.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"MEDASSIST_{name}", default)


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(Path.home() / ".local/share/medassist"))))

    # Local inference
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: _env("MODEL", "qwen3:4b"))
    # 8K keeps qwen3:4b fully on a 6 GB GPU; 16K spilled 21% of layers to CPU.
    num_ctx: int = field(default_factory=lambda: _env_int("NUM_CTX", 8192))
    model_timeout_s: int = field(default_factory=lambda: _env_int("MODEL_TIMEOUT", 600))
    # The installed qwen3:4b (2507 Thinking build) always reasons; with think=True
    # Ollama returns reasoning separately so replies stay clean.
    think: bool = field(default_factory=lambda: _env_bool("THINK", True))
    # JSON tasks use a raw Qwen prompt with an empty <think> block (no reasoning).
    raw_json_no_think: bool = field(default_factory=lambda: _env_bool("RAW_JSON_NO_THINK", True))
    agent_backend: str = field(default_factory=lambda: _env("AGENT_BACKEND", "local"))  # local | hermes
    agent_max_steps: int = field(default_factory=lambda: _env_int("AGENT_MAX_STEPS", 6))

    # Evidence gateway (separate process; the only component with internet egress)
    gateway_url: str = field(default_factory=lambda: _env("GATEWAY_URL", "http://127.0.0.1:8601"))
    mcp_url: str = field(default_factory=lambda: _env("MCP_URL", "https://pubmed.caseyjhand.com/mcp"))
    mcp_allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(h.strip() for h in _env("MCP_ALLOWED_HOSTS", "pubmed.caseyjhand.com").split(","))
    )
    gateway_timeout_s: int = field(default_factory=lambda: _env_int("GATEWAY_TIMEOUT", 30))
    gateway_max_response_bytes: int = field(default_factory=lambda: _env_int("GATEWAY_MAX_RESPONSE", 4_000_000))

    # Limits (spec section 15)
    max_upload_bytes: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 25) * 1024 * 1024)
    max_pdf_pages: int = field(default_factory=lambda: _env_int("MAX_PDF_PAGES", 50))
    max_sheet_rows: int = field(default_factory=lambda: _env_int("MAX_SHEET_ROWS", 100_000))
    max_decompressed_bytes: int = field(default_factory=lambda: _env_int("MAX_DECOMPRESSED_MB", 200) * 1024 * 1024)
    max_image_pixels: int = field(default_factory=lambda: _env_int("MAX_IMAGE_MEGAPIXELS", 60) * 1_000_000)

    # Security
    session_hours: int = field(default_factory=lambda: _env_int("SESSION_HOURS", 10))
    pairing_ttl_s: int = field(default_factory=lambda: _env_int("PAIRING_TTL", 300))
    phone_session_minutes: int = field(default_factory=lambda: _env_int("PHONE_SESSION_MINUTES", 60))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip() for o in _env("ALLOWED_ORIGINS", "").split(",") if o.strip())
    )
    secure_cookies: bool = field(default_factory=lambda: _env_bool("SECURE_COOKIES", False))
    retain_capture_originals: bool = field(default_factory=lambda: _env_bool("RETAIN_CAPTURE_ORIGINALS", True))
    retention_days: int = field(default_factory=lambda: _env_int("RETENTION_DAYS", 0))  # 0 = set per clinic policy
    network_guard: bool = field(default_factory=lambda: _env_bool("NETWORK_GUARD", True))

    # OCR
    ocr_engine: str = field(default_factory=lambda: _env("OCR_ENGINE", "paddle"))  # paddle | none
    ocr_lang: str = field(default_factory=lambda: _env("OCR_LANG", "en"))
    ocr_structure: bool = field(default_factory=lambda: _env_bool("OCR_STRUCTURE", False))

    # Reports
    pdf_font_path: str = field(default_factory=lambda: _env("PDF_FONT", ""))

    frontend_dist: Path = field(
        default_factory=lambda: Path(_env("FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")))
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "medassist.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    def gateway_secret(self) -> str:
        """Shared secret authenticating the API to the local gateway."""
        env = os.environ.get("MEDASSIST_GATEWAY_SECRET")
        if env:
            return env
        p = self.data_dir / "gateway.secret"
        if not p.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            p.write_text(secrets.token_urlsafe(32))
            p.chmod(0o600)
        return p.read_text().strip()


settings = Settings()
