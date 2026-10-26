"""Participant schema validation, identifiers, and storage errors."""

from __future__ import annotations

import re


STUDY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
PARTICIPANT_DATA_VERSION = 2
DEFAULT_PRICING_POLICY_ID = "study-2026-09-standard-v1"


class StorageError(RuntimeError):
    """Raised when persisted prototype data cannot be read or written safely."""


class ParticipantDataError(ValueError):
    """Raised when participant data violates the current storage format."""


def validate_participant_state(state: dict) -> None:
    if state.get("schemaVersion") != PARTICIPANT_DATA_VERSION:
        raise ParticipantDataError(
            f"participant data format must be version {PARTICIPANT_DATA_VERSION}"
        )
    _require_nonempty_string(state, "participantId", "participant")
    if not isinstance(state.get("creditLimitUsd"), (int, float)) or state["creditLimitUsd"] < 0:
        raise ParticipantDataError("creditLimitUsd must be a non-negative number")
    _require_nonempty_string(state, "pricingPolicyId", "participant")
    for collection in ("conversations", "messages", "modelCalls", "webSearches"):
        if not isinstance(state.get(collection), list):
            raise ParticipantDataError(f"{collection} must be an array")

    conversations = _index_unique(state["conversations"], "conversations")
    messages = _index_unique(state["messages"], "messages")
    calls = _index_unique(state["modelCalls"], "modelCalls")
    _index_unique(state["webSearches"], "webSearches")

    for conversation in conversations.values():
        _require_nonempty_string(conversation, "title", "conversation")
        _require_nonempty_string(conversation, "createdAt", "conversation")
        settings = conversation.get("settings")
        if not isinstance(settings, dict):
            raise ParticipantDataError("conversation.settings must be an object")
        _require_nonempty_string(settings, "modelId", "conversation.settings")
        _require_nonempty_string(settings, "reasoningEffort", "conversation.settings")
        if not isinstance(settings.get("thinkingEnabled"), bool):
            raise ParticipantDataError("conversation.settings.thinkingEnabled must be boolean")

    for message in messages.values():
        if message.get("conversationId") not in conversations:
            raise ParticipantDataError("message.conversationId must reference a conversation")
        if message.get("role") not in {"user", "assistant"}:
            raise ParticipantDataError("message.role must be user or assistant")
        if not isinstance(message.get("content"), str):
            raise ParticipantDataError("message.content must be a string")
        reasoning_summary = message.get("reasoningSummary")
        if reasoning_summary is not None:
            if message.get("role") != "assistant" or not isinstance(reasoning_summary, list):
                raise ParticipantDataError("reasoningSummary must be an assistant message array")
            indexes: set[int] = set()
            for block in reasoning_summary:
                if not isinstance(block, dict):
                    raise ParticipantDataError("reasoningSummary entries must be objects")
                index = block.get("index")
                if not isinstance(index, int) or index < 0 or index in indexes:
                    raise ParticipantDataError("reasoningSummary indexes must be unique non-negative integers")
                indexes.add(index)
                _require_nonempty_string(block, "text", "reasoningSummary")
        reasoning_duration = message.get("reasoningDurationMs")
        if reasoning_duration is not None and (
            message.get("role") != "assistant"
            or not isinstance(reasoning_duration, int)
            or isinstance(reasoning_duration, bool)
            or reasoning_duration < 0
        ):
            raise ParticipantDataError("reasoningDurationMs must be a non-negative assistant message integer")
        response_parts = message.get("responseParts")
        if response_parts is not None:
            if message.get("role") != "assistant" or not isinstance(response_parts, list):
                raise ParticipantDataError("responseParts must be an assistant message array")
            for part in _index_unique(response_parts, "responseParts").values():
                part_type = part.get("type")
                if part_type not in {"text", "reasoning", "search"}:
                    raise ParticipantDataError("responsePart.type is invalid")
                if part_type in {"text", "reasoning"}:
                    _require_nonempty_string(part, "text", "responsePart")
                if "phase" in part and (part_type != "text" or part["phase"] not in {"commentary", "final_answer"}):
                    raise ParticipantDataError("text responsePart.phase is invalid")
                if part_type == "reasoning" and (
                    not isinstance(part.get("index"), int) or part["index"] < 0
                ):
                    raise ParticipantDataError("reasoning responsePart.index must be non-negative")
                if part_type == "search":
                    if not isinstance(part.get("sources"), list):
                        raise ParticipantDataError("search responsePart.sources must be an array")
                    for source in part["sources"]:
                        if not isinstance(source, dict):
                            raise ParticipantDataError("responsePart sources must be objects")
                        _require_nonempty_string(source, "url", "responsePart source")
        citations = message.get("citations")
        if citations is not None:
            if message.get("role") != "assistant" or not isinstance(citations, list):
                raise ParticipantDataError("citations must be an assistant message array")
            for citation in citations:
                if not isinstance(citation, dict):
                    raise ParticipantDataError("message citations must be objects")
                _require_nonempty_string(citation, "url", "message citation")
                if citation.get("placement") not in {"inline", "footer"}:
                    raise ParticipantDataError("message citation placement is invalid")
                if citation["placement"] == "inline":
                    for key in ("startIndex", "endIndex"):
                        if not isinstance(citation.get(key), int) or citation[key] < 0:
                            raise ParticipantDataError(f"message citation {key} must be non-negative")
        _require_nonempty_string(message, "createdAt", "message")

    started_by_conversation: set[str] = set()
    client_request_ids: set[str] = set()
    for call in calls.values():
        conversation_id = call.get("conversationId")
        if conversation_id not in conversations:
            raise ParticipantDataError("modelCall.conversationId must reference a conversation")
        request = call.get("request")
        if not isinstance(request, dict):
            raise ParticipantDataError("modelCall.request must be an object")
        for key in ("modelId", "reasoningEffort"):
            _require_nonempty_string(request, key, "modelCall.request")
        for key in ("thinkingEnabled", "webSearchEnabled"):
            if not isinstance(request.get(key), bool):
                raise ParticipantDataError(f"modelCall.request.{key} must be boolean")
        if "webSearchMode" in request and request["webSearchMode"] not in {"auto", "required"}:
            raise ParticipantDataError("modelCall.request.webSearchMode must be auto or required")
        if "clientRequestId" not in call or "clientInstanceId" not in call:
            raise ParticipantDataError("modelCall client request identifiers are required")
        client_request_id = call.get("clientRequestId")
        if client_request_id is not None:
            if not isinstance(client_request_id, str) or not client_request_id:
                raise ParticipantDataError("modelCall.clientRequestId must be null or a non-empty string")
            if client_request_id in client_request_ids:
                raise ParticipantDataError("modelCall.clientRequestId must be unique")
            client_request_ids.add(client_request_id)
        client_instance_id = call.get("clientInstanceId")
        if client_instance_id is not None and (not isinstance(client_instance_id, str) or not client_instance_id):
            raise ParticipantDataError("modelCall.clientInstanceId must be null or a non-empty string")
        for field, expected_role in (("inputMessageId", "user"), ("outputMessageId", "assistant")):
            message_id = call.get(field)
            if message_id is None:
                continue
            message = messages.get(message_id)
            if not message or message["conversationId"] != conversation_id:
                raise ParticipantDataError(f"modelCall.{field} must reference a message in its conversation")
            if message["role"] != expected_role:
                raise ParticipantDataError(f"modelCall.{field} must reference a {expected_role} message")
        status = call.get("status")
        if status not in {"started", "complete", "failed"}:
            raise ParticipantDataError("modelCall.status is invalid")
        if status == "started":
            if conversation_id in started_by_conversation:
                raise ParticipantDataError("only one started modelCall is allowed per conversation")
            started_by_conversation.add(conversation_id)
            if call.get("completedAt") is not None or call.get("outputMessageId") is not None:
                raise ParticipantDataError("started modelCall cannot have completion data")
        elif status == "complete":
            if not call.get("completedAt"):
                raise ParticipantDataError("complete modelCall must have completedAt")
            if "usage" not in call:
                raise ParticipantDataError("complete modelCall must have usage")
            if call.get("outputMessageId") is None:
                raise ParticipantDataError("complete modelCall must reference an output message")
        elif status == "failed":
            if not call.get("completedAt") or not call.get("errorCode"):
                raise ParticipantDataError("failed modelCall must have completedAt and errorCode")
        provider_state = call.get("providerState")
        if provider_state is not None:
            if status != "complete" or not isinstance(provider_state, dict):
                raise ParticipantDataError("providerState must belong to a complete modelCall")
            if provider_state.get("schemaVersion") != 1:
                raise ParticipantDataError("providerState.schemaVersion must be 1")
            _require_nonempty_string(provider_state, "provider", "providerState")
            _require_nonempty_string(provider_state, "modelId", "providerState")
            if provider_state["modelId"] != request["modelId"]:
                raise ParticipantDataError("providerState.modelId must match modelCall.request.modelId")
            if not isinstance(provider_state.get("payload"), dict):
                raise ParticipantDataError("providerState.payload must be an object")
        _require_nonempty_string(call, "startedAt", "modelCall")

    for search in state["webSearches"]:
        model_call = calls.get(search.get("modelCallId"))
        if model_call is None:
            raise ParticipantDataError("webSearch.modelCallId must reference a modelCall")
        for collection in ("executions", "sources", "citations"):
            if not isinstance(search.get(collection), list):
                raise ParticipantDataError(f"webSearch.{collection} must be an array")
        _index_unique(search["executions"], "webSearch.executions")
        sources = _index_unique(search["sources"], "webSearch.sources")
        citations = _index_unique(search["citations"], "webSearch.citations")
        for citation in citations.values():
            if citation.get("sourceId") not in sources:
                raise ParticipantDataError("citation.sourceId must reference a source")
            message = messages.get(citation.get("messageId"))
            if message is None or message.get("role") != "assistant":
                raise ParticipantDataError("citation.messageId must reference a message")
            if model_call.get("outputMessageId") and citation["messageId"] != model_call["outputMessageId"]:
                raise ParticipantDataError("citation must reference its modelCall output message")


def _index_unique(rows: list[dict], collection: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ParticipantDataError(f"{collection} entries must be objects")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ParticipantDataError(f"{collection} IDs must be non-empty strings")
        if row_id in index:
            raise ParticipantDataError(f"duplicate {collection} ID: {row_id}")
        index[row_id] = row
    return index


def _require_nonempty_string(value: dict, key: str, owner: str) -> None:
    if not isinstance(value.get(key), str) or not value[key]:
        raise ParticipantDataError(f"{owner}.{key} must be a non-empty string")


def validate_study_id(study_id: str) -> str:
    normalized = (study_id or "").strip()
    if not STUDY_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid study ID")
    return normalized
