"""Conversation and model endpoints and generation transport."""
from __future__ import annotations

import uuid

from flask import Response, jsonify, request, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge

from api.auth import current_study_id, login_required
from api.transport import error, sse_event, public_stream_event, public_model_error
from chat.generation import GenerationRequest, GenerationSession
from chat.records import public_conversation, participant_usage, utc_now
from files.errors import AttachmentError
from models.catalog import (
    DEFAULT_MODEL_ID,
    ModelError,
    get_model,
    get_reasoning_effort,
    get_thinking_enabled,
    get_web_search_mode,
    list_models,
    random_initial_settings,
)
from models.dispatch import stream_response
from persistence.participant_schema import StorageError


def register_routes(app):
    store = app.extensions["participant_store"]
    attachments = app.extensions["attachments"]
    budget_ledger = app.extensions["live_budget_ledger"]

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/models")
    @login_required
    def models():
        return jsonify(
            {
                "models": list_models(),
                "initialSettings": initial_model_assignment(current_study_id()),
                "maxConcurrentGenerations": app.config["MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT"],
            }
        )

    def initial_model_assignment(study_id: str) -> dict | None:
        """Return the participant's random starting setting, drawing it once.

        Only a participant with no conversations yet gets a draw; anyone who
        already chatted has a starting point of their own, and assigning one
        after the fact would put a meaningless row in the research data.
        """
        reader = store.read(study_id)
        assignment = reader.initial_model_assignment()
        if assignment is not None or reader.conversation_count() > 0:
            return assignment
        with store.write(study_id) as transaction:
            # Re-check under the write lock: two tabs can boot at once.
            assignment = transaction.initial_model_assignment()
            if assignment is None and transaction.conversation_count() == 0:
                assignment = {**random_initial_settings(), "assignedAt": utc_now()}
                transaction.insert_initial_model_assignment(assignment)
        return assignment

    @app.get("/api/usage")
    @login_required
    def usage():
        return jsonify(participant_usage(store.read(current_study_id())))

    @app.get("/api/conversations")
    @login_required
    def conversations():
        transaction = store.read(current_study_id())
        # Two indexed aggregates. No message bodies, no provider state.
        message_stats = transaction.message_stats_by_conversation()
        call_timestamps = transaction.call_timestamps_by_conversation()
        generating_ids = {
            call["conversationId"] for call in transaction.active_model_calls()
        }
        rows = sorted(
            (
                public_conversation(
                    conversation,
                    call_timestamps=call_timestamps.get(conversation["id"], []),
                    message_stats=message_stats.get(conversation["id"], (0, None)),
                    is_generating=conversation["id"] in generating_ids,
                )
                for conversation in transaction.conversations()
            ),
            key=lambda row: row["updatedAt"],
            reverse=True,
        )
        return jsonify({"conversations": rows})

    @app.get("/api/conversations/<conversation_id>/messages")
    @login_required
    def conversation_messages(conversation_id: str):
        transaction = store.read(current_study_id())
        if transaction.conversation(conversation_id) is None:
            return error("CONVERSATION_NOT_FOUND", "대화를 찾을 수 없습니다.", 404)
        return jsonify({"messages": transaction.messages(conversation_id)})

    @app.post("/api/conversations")
    @login_required
    def create_conversation():
        payload = request.get_json(silent=True) or {}
        model_id = str(payload.get("modelId", DEFAULT_MODEL_ID))
        try:
            model = get_model(model_id)
            reasoning_effort = get_reasoning_effort(model, payload.get("reasoningEffort"))
        except ModelError:
            return error("INVALID_MODEL_SETTINGS", "모델 또는 추론 단계 설정이 올바르지 않습니다.", 400)
        now = utc_now()
        conversation = {
            "id": str(uuid.uuid4()),
            "title": "새 대화",
            "settings": {
                "modelId": model_id,
                "reasoningEffort": reasoning_effort,
                "thinkingEnabled": get_thinking_enabled(model, payload.get("thinkingEnabled")),
            },
            "createdAt": now,
        }
        with store.write(current_study_id()) as transaction:
            if (
                transaction.conversation_count()
                >= app.config["MAX_CONVERSATIONS_PER_PARTICIPANT"]
            ):
                return error(
                    "CONVERSATION_LIMIT_EXCEEDED",
                    "만들 수 있는 대화 수 한도에 도달했습니다.",
                    409,
                )
            transaction.insert_conversation(conversation)
        return jsonify({"conversation": public_conversation(conversation, [])}), 201

    @app.patch("/api/conversations/<conversation_id>")
    @login_required
    def update_conversation(conversation_id: str):
        payload = request.get_json(silent=True) or {}

        def mutate(transaction):
            conversation = transaction.conversation(conversation_id)
            if not conversation:
                return None
            settings = conversation["settings"]
            if "modelId" in payload:
                model = get_model(str(payload["modelId"]))
                current_model = get_model(settings["modelId"])
                if transaction.has_messages(conversation_id) and current_model.provider != model.provider:
                    raise ModelError("PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION")
                effort = get_reasoning_effort(model, payload.get("reasoningEffort"))
                settings["modelId"] = model.id
                settings["reasoningEffort"] = effort
                if "thinkingEnabled" in payload:
                    settings["thinkingEnabled"] = get_thinking_enabled(model, payload["thinkingEnabled"])
                elif model.provider == "anthropic" and current_model.provider == "anthropic":
                    settings["thinkingEnabled"] = settings.get("thinkingEnabled", True)
                else:
                    settings["thinkingEnabled"] = get_thinking_enabled(model)
            elif "reasoningEffort" in payload:
                model = get_model(settings["modelId"])
                settings["reasoningEffort"] = get_reasoning_effort(model, payload["reasoningEffort"])
            if "thinkingEnabled" in payload and "modelId" not in payload:
                model = get_model(settings["modelId"])
                settings["thinkingEnabled"] = get_thinking_enabled(model, payload["thinkingEnabled"])
            transaction.update_conversation(conversation_id, settings=settings)
            return conversation

        try:
            with store.write(current_study_id()) as transaction:
                conversation = mutate(transaction)
                messages = transaction.messages(conversation_id) if conversation else []
                call_timestamps = (
                    transaction.conversation_call_timestamps(conversation_id)
                    if conversation
                    else []
                )
        except ModelError as exc:
            if str(exc) == "PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION":
                return error(
                    "PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION",
                    "다른 모델 회사로 전환하려면 새 대화를 시작해주세요.",
                    409,
                )
            return error("INVALID_MODEL_SETTINGS", "모델 또는 추론 단계 설정이 올바르지 않습니다.", 400)
        if not conversation:
            return error("CONVERSATION_NOT_FOUND", "대화를 찾을 수 없습니다.", 404)
        return jsonify(
            {"conversation": public_conversation(conversation, messages, call_timestamps)}
        )

    @app.post("/api/conversations/<conversation_id>/messages")
    @login_required
    def send_message(conversation_id: str):
        payload = request.get_json(silent=True) or {}
        content = str(payload.get("content", "")).strip()
        attachment_ids = payload.get("attachmentIds", [])
        model_id = str(payload.get("modelId", ""))
        reasoning_effort = payload.get("reasoningEffort")
        thinking_enabled = payload.get("thinkingEnabled")
        try:
            web_search_mode = get_web_search_mode(
                payload.get("webSearchMode", payload.get("webSearchEnabled", False))
            )
        except ModelError:
            return error("INVALID_WEB_SEARCH_MODE", "웹 검색 설정이 올바르지 않습니다.", 400)
        client_request_id = str(payload.get("clientRequestId") or uuid.uuid4())
        client_instance_id = str(payload.get("clientInstanceId") or uuid.uuid4())
        if not content and not attachment_ids:
            return error("EMPTY_MESSAGE", "메시지를 입력해주세요.", 400)
        try:
            model = get_model(model_id)
            reasoning_effort = get_reasoning_effort(model, reasoning_effort)
            thinking_enabled = get_thinking_enabled(model, thinking_enabled)
        except ModelError:
            return error("INVALID_MODEL_SETTINGS", "모델 또는 추론 단계 설정이 올바르지 않습니다.", 400)
        if web_search_mode == "required" and not model.supports_web_search:
            return error("MODEL_CAPABILITY_UNSUPPORTED", "이 모델은 웹 검색을 지원하지 않습니다.", 400)
        if web_search_mode == "required" and not model.supports_forced_web_search:
            return error(
                "MODEL_SETTING_COMBINATION_UNSUPPORTED",
                "이 모델은 웹 검색 필수 모드를 지원하지 않습니다. 웹 검색 자동을 사용해주세요.",
                400,
            )
        if (
            web_search_mode == "required"
            and model.provider == "anthropic"
            and thinking_enabled
            and not model.adaptive_thinking
        ):
            return error(
                "MODEL_SETTING_COMBINATION_UNSUPPORTED",
                "이 Claude 모델에서는 Thinking을 끈 뒤 웹 검색 필수를 사용할 수 있습니다.",
                400,
            )

        generation = GenerationSession(
            GenerationRequest(
                study_id=current_study_id(), conversation_id=conversation_id,
                content=content, attachment_ids=attachment_ids, model=model,
                reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
                web_search_mode=web_search_mode, client_request_id=client_request_id,
                client_instance_id=client_instance_id,
            ),
            config=app.config, store=store, attachments=attachments,
            budget_ledger=budget_ledger, stream_response=stream_response,
        )
        try:
            generation.start()
        except ModelError as exc:
            code = str(exc)
            if code == "CONVERSATION_NOT_FOUND":
                return error(code, "대화를 찾을 수 없습니다.", 404)
            if code == "CONVERSATION_BUSY":
                return error(code, "이 대화에서는 이미 응답을 생성하고 있습니다. 완료 후 다시 시도해주세요.", 409)
            if code == "CONCURRENT_CALL_LIMIT_EXCEEDED":
                limit = app.config["MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT"]
                return error(
                    code,
                    f"동시에 생성할 수 있는 응답은 최대 {limit}개입니다. 진행 중인 응답이 끝난 뒤 다시 시도해주세요.",
                    429,
                )
            if code == "GLOBAL_CONCURRENT_CALL_LIMIT_EXCEEDED":
                return error(
                    code,
                    "서버가 동시에 처리할 수 있는 응답 수에 도달했습니다. 잠시 후 다시 시도해주세요.",
                    503,
                )
            if code == "DUPLICATE_REQUEST":
                return error(code, "이미 처리된 메시지 요청입니다. 대화 기록을 새로고침해주세요.", 409)
            if code == "PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION":
                return error(code, "다른 모델 회사로 전환하려면 새 대화를 시작해주세요.", 409)
            if code == "CREDIT_LIMIT_EXCEEDED":
                return error(code, "사용 가능한 크레딧이 부족합니다.", 402)
            if code == "PARTICIPANT_STORAGE_LIMIT_EXCEEDED":
                return error(
                    code,
                    "저장 가능한 대화 내용 한도에 도달했습니다.",
                    409,
                )
            return error("INVALID_MODEL_SETTINGS", "모델 또는 추론 단계 설정이 올바르지 않습니다.", 400)
        def log_provider_error(code: str) -> None:
            if code in {"PROVIDER_SDK_UNAVAILABLE", "PROVIDER_REQUEST_FAILED"}:
                app.logger.exception("Model provider request failed for %s", model.provider_label)

        if "text/event-stream" in request.headers.get("Accept", ""):
            @stream_with_context
            def event_stream():
                events = generation.events()
                try:
                    yield sse_event("message_start", {
                        "userMessage": generation.user_message,
                        "assistantMessage": generation.placeholder(),
                    })
                    yield sse_event("status", {"phase": "thinking"})
                    for event in events:
                        public_event = public_stream_event(event)
                        if public_event is not None:
                            yield sse_event(*public_event)
                except GeneratorExit:
                    # Let the active stream preserve its budget cutoff reason first.
                    events.close()
                    generation.disconnect()
                    raise
                except ModelError as exc:
                    code = str(exc)
                    log_provider_error(code)
                    public_code, message, status = public_model_error(code)
                    yield sse_event("error", {
                        "error": {"code": public_code, "message": message}, "status": status,
                    })
                finally:
                    events.close()

            response = Response(
                event_stream(), mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            )

            def settle_unconsumed_response():
                try:
                    generation.disconnect()
                except Exception:  # pragma: no cover - WSGI close-path safeguard
                    app.logger.exception("Could not settle disconnected generation %s", generation.call_id)

            response.call_on_close(settle_unconsumed_response)
            return response

        events = generation.events()
        try:
            for event in events:
                if event['type'] == 'done':
                    return jsonify(event['payload'])
        except ModelError as exc:
            code = str(exc)
            log_provider_error(code)
            return error(*public_model_error(code))
        finally:
            events.close()


    @app.errorhandler(StorageError)
    def handle_storage_error(_exc):
        return error("STORAGE_ERROR", "데이터를 저장하거나 불러오지 못했습니다.", 500)

    @app.errorhandler(AttachmentError)
    def handle_attachment_error(exc):
        return error(exc.code, str(exc), exc.status)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(_exc):
        return error(
            "REQUEST_TOO_LARGE",
            "요청이 서버의 크기 제한을 초과했습니다. 더 짧은 내용으로 다시 시도해주세요.",
            413,
        )
