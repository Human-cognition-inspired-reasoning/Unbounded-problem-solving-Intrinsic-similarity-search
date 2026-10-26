"""Research account authentication routes."""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, current_app, jsonify, request, session


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def current_study_id() -> str | None:
    return session.get("study_id")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        study_id = current_study_id()
        store = current_app.extensions["participant_store"]
        if not study_id or not store.account_is_active(study_id):
            session.clear()
            return jsonify({"error": {"code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}}), 401
        return view(*args, **kwargs)

    return wrapped


@auth_bp.post("/login")
def login():
    payload = request.get_json(silent=True) or {}
    study_id = str(payload.get("studyId", "")).strip()
    password = str(payload.get("password", ""))
    store = current_app.extensions["participant_store"]
    auth_result = store.authenticate_rate_limited(
        study_id,
        password,
        max_attempts=current_app.config["LOGIN_MAX_ATTEMPTS"],
        window_seconds=current_app.config["LOGIN_WINDOW_SECONDS"],
    )
    if auth_result == "limited":
        return jsonify(
            {
                "error": {
                    "code": "LOGIN_RATE_LIMITED",
                    "message": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해주세요.",
                }
            }
        ), 429
    if auth_result != "authenticated":
        return jsonify({"error": {"code": "INVALID_CREDENTIALS", "message": "ID 또는 비밀번호를 확인해주세요."}}), 401
    session.clear()
    session["study_id"] = study_id
    return jsonify({"participant": {"studyId": study_id}})


@auth_bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@auth_bp.get("/me")
def me():
    study_id = current_study_id()
    return jsonify({"participant": {"studyId": study_id} if study_id else None})
