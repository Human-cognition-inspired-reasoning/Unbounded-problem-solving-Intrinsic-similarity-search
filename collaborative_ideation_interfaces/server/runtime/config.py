"""Load server settings explicitly; importing this module does not load .env or open data."""
from __future__ import annotations

import os
from pathlib import Path
import secrets

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]


def environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(overrides: dict | None = None) -> dict:
    load_dotenv(BASE_DIR / ".env")
    seed_demo_account = environment_flag("SEED_DEMO_ACCOUNT", False)
    configured_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
    settings = dict(
        SECRET_KEY=configured_secret or secrets.token_hex(32),
        DATA_DIR=os.getenv("DATA_DIR", str(BASE_DIR / "data")),
        DEFAULT_CREDIT_LIMIT_USD=float(os.getenv("DEFAULT_CREDIT_LIMIT_USD", "10.00")),
        SEED_DEMO_ACCOUNT=seed_demo_account,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=environment_flag(
            "SESSION_COOKIE_SECURE", not seed_demo_account
        ),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_JSON_REQUEST_BYTES", str(1024 * 1024))),
        MODEL_CALL_STALE_AFTER_SECONDS=int(os.getenv("MODEL_CALL_STALE_AFTER_SECONDS", "3600")),
        MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT=max(
            1,
            int(os.getenv("MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT", "3")),
        ),
        MAX_GLOBAL_CONCURRENT_MODEL_CALLS=max(
            1, int(os.getenv("MAX_GLOBAL_CONCURRENT_MODEL_CALLS", "36"))
        ),
        LOGIN_MAX_ATTEMPTS=max(1, int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))),
        LOGIN_WINDOW_SECONDS=max(1, int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))),
        MAX_CONVERSATIONS_PER_PARTICIPANT=max(
            1, int(os.getenv("MAX_CONVERSATIONS_PER_PARTICIPANT", "500"))
        ),
        MAX_STORED_MESSAGE_CHARS_PER_PARTICIPANT=max(
            1, int(os.getenv("MAX_STORED_MESSAGE_CHARS_PER_PARTICIPANT", "5000000"))
        ),
        DATABASE_PATH=os.getenv("DATABASE_PATH", ""),
    )
    if overrides:
        settings.update(overrides)
    return settings


def configure_attachments(settings):
    defaults = dict(
        ATTACHMENT_STORAGE='local' if settings['SEED_DEMO_ACCOUNT'] else 'off',
        ATTACHMENT_MAX_FILE_BYTES=10 * 1024**2,
        ATTACHMENT_MAX_DERIVED_BYTES=20 * 1024**2,
        ATTACHMENT_PARTICIPANT_BYTES=100 * 1024**2,
        ATTACHMENT_MAX_FILES=3,
        ATTACHMENT_MAX_PAGES=50,
        ATTACHMENT_MAX_ROWS=1000,
        ATTACHMENT_CONTEXT_BYTES=20 * 1024**2,
        ATTACHMENT_CONTEXT_TOKENS=100000,
        ATTACHMENT_CONVERSION_TIMEOUT=45,
        ATTACHMENT_FONT_PATH='', LIBREOFFICE_BIN='',
    )
    for key, value in defaults.items():
        settings.setdefault(key, type(value)(os.getenv(key, value)))
    return settings
