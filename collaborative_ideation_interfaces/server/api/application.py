"""Flask factory. Importing it does not create an application or database."""
from flask import Flask

from api.auth import auth_bp
from api.routes import register_routes
from chat.budget import LiveBudgetLedger
from files.routes import blueprint as attachment_blueprint
from files.service import AttachmentService
from runtime.config import load_settings
from runtime.services import open_store


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask("app")
    app.config.update(load_settings(test_config))
    store = open_store(app.config)
    if app.config["SEED_DEMO_ACCOUNT"]:
        store.ensure_account("P-001", "prototype", app.config["DEFAULT_CREDIT_LIMIT_USD"])
    app.extensions["participant_store"] = store
    app.extensions["live_budget_ledger"] = LiveBudgetLedger()
    attachments = AttachmentService(app.config, store)
    app.extensions["attachments"] = attachments
    app.register_blueprint(auth_bp)
    app.register_blueprint(attachment_blueprint(attachments))
    register_routes(app)
    return app
