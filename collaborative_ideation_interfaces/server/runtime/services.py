"""Construct storage/services for web requests and maintenance without starting Flask."""
from pathlib import Path

from files.service import AttachmentService
from persistence.sqlite import SqliteStore


def open_store(settings):
    database = settings.get("DATABASE_PATH") or str(Path(settings["DATA_DIR"]) / "study.db")
    return SqliteStore(database, settings["DEFAULT_CREDIT_LIMIT_USD"])


def open_attachment_service(settings):
    return AttachmentService(settings, open_store(settings))
