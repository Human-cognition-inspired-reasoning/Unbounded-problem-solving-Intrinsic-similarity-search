"""One generation's transaction, budget, provider stream and settlement lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import threading
import uuid

from chat.records import (
    parse_utc_timestamp,
    participant_usage,
    persisted_web_search,
    public_message_citations,
    public_model_call,
    utc_now,
)
from chat.responses import StreamingGenerationAccumulator
from chat.responses import finalized_response_parts
from files.errors import AttachmentError
from models.catalog import ModelError, ModelSpec, get_model

CONVERSATION_TITLE_PREVIEW_LENGTH = 20


@dataclass(frozen=True)
class GenerationRequest:
    study_id: str
    conversation_id: str
    content: str
    attachment_ids: list[str]
    model: ModelSpec
    reasoning_effort: str
    thinking_enabled: bool
    web_search_mode: str
    client_request_id: str
    client_instance_id: str

    @property
    def model_id(self):
        return self.model.id


class GenerationSession:
    """Per-request state. Construction does not start or charge a provider call."""

    def __init__(self, request, *, config, store, attachments, budget_ledger, stream_response):
        self.request = request
        self.config = config
        self.store = store
        self.attachments = attachments
        self.budget_ledger = budget_ledger
        self.stream_response = stream_response
        self.now = utc_now()
        self.now_datetime = parse_utc_timestamp(self.now)
        self.user_message = {
            "id": str(uuid.uuid4()), "role": "user", "content": request.content,
            "createdAt": self.now,
        }
        self.assistant_id = str(uuid.uuid4())
        self.call_id = str(uuid.uuid4())
        self.settlement_lock = threading.Lock()
        self.settlement = {"done": False, "payload": None}
        self.stream_state = {"provider_started": False}

    def start(self):
        try:
            with self.store.write(self.request.study_id) as transaction:
                self._start_call(transaction)
        except BaseException:
            self.budget_ledger.discard(self.request.study_id, self.call_id)
            raise

    def placeholder(self):
        return {
            "id": self.assistant_id, "role": "assistant", "content": "",
            "createdAt": self.now, "startedAt": self.now,
            "modelId": self.request.model_id,
            "reasoningEffort": self.request.reasoning_effort,
            "thinkingEnabled": self.request.thinking_enabled,
            "streaming": True, "phase": "thinking",
            "reasoningSummary": [], "responseParts": [], "citations": [],
        }

    def events(self):
        """Yield provider progress and one durable completion for either HTTP format."""
        provider_events = self._guarded_provider_events()
        budget_exhausted = False
        try:
            generation = None
            for event, exhausted in provider_events:
                budget_exhausted = budget_exhausted or exhausted
                if event['type'] == 'done':
                    generation = event['generation']
                else:
                    yield event
            if budget_exhausted:
                payload = self._finish_generation(
                    self.accumulator.partial_generation('credit_exhausted'), 'credit_exhausted', 'estimated',
                )
            else:
                if generation is None:
                    raise ModelError('EMPTY_PROVIDER_RESPONSE')
                generation['responseParts'] = self.accumulator.response_parts
                payload = self._finish_generation(generation)
            yield {'type': 'done', 'payload': payload}
        except GeneratorExit:
            self.disconnect(budget_exhausted)
            raise
        except ModelError as exc:
            self._fail_generation(str(exc))
            raise
        finally:
            provider_events.close()

    def _start_call(self, transaction):
        current = transaction.conversation(self.request.conversation_id)
        if not current:
            raise ModelError("CONVERSATION_NOT_FOUND")
        if transaction.client_request_exists(self.request.client_request_id):
            raise ModelError("DUPLICATE_REQUEST")

        stale_after_seconds = max(1, int(self.config["MODEL_CALL_STALE_AFTER_SECONDS"]))
        fresh_after = (
            (self.now_datetime - timedelta(seconds=stale_after_seconds))
            .isoformat()
            .replace("+00:00", "Z")
            if self.now_datetime
            else None
        )
        active_calls = []
        active_conversation_calls = []
        for existing_call in transaction.active_model_calls():
            started_at = parse_utc_timestamp(existing_call.get("startedAt"))
            is_stale = bool(
                started_at
                and self.now_datetime
                and (self.now_datetime - started_at).total_seconds() >= stale_after_seconds
            )
            if is_stale:
                transaction.update_model_call(
                    existing_call["id"],
                    {"status": "failed", "errorCode": "STALE_MODEL_CALL", "completedAt": self.now},
                )
            else:
                active_calls.append(existing_call)
                if existing_call.get("conversationId") == self.request.conversation_id:
                    active_conversation_calls.append(existing_call)
        if active_conversation_calls:
            raise ModelError("CONVERSATION_BUSY")
        if (
            len(active_calls)
            >= self.config["MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT"]
        ):
            raise ModelError("CONCURRENT_CALL_LIMIT_EXCEEDED")
        if (
            transaction.global_active_model_call_count(fresh_after)
            >= self.config["MAX_GLOBAL_CONCURRENT_MODEL_CALLS"]
        ):
            raise ModelError("GLOBAL_CONCURRENT_CALL_LIMIT_EXCEEDED")

        if (
            transaction.stored_message_char_count() + len(self.request.content)
            > self.config["MAX_STORED_MESSAGE_CHARS_PER_PARTICIPANT"]
        ):
            raise ModelError("PARTICIPANT_STORAGE_LIMIT_EXCEEDED")

        if transaction.has_messages(self.request.conversation_id):
            conversation_model = get_model(current["settings"]["modelId"])
            if conversation_model.provider != self.request.model.provider:
                raise ModelError("PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION")
        usage_snapshot = participant_usage(transaction)
        remaining_usd = usage_snapshot["remainingUsd"]
        if remaining_usd <= 0:
            raise ModelError("CREDIT_LIMIT_EXCEEDED")

        self.user_message["conversationId"] = self.request.conversation_id
        self.attachments.bind(transaction, self.request.attachment_ids, self.user_message)
        context_messages = [*transaction.messages(self.request.conversation_id), self.user_message]
        self.attachments.validate_context(context_messages)
        if any(m.get('attachments') for m in context_messages):
            estimate = StreamingGenerationAccumulator(context_messages, self.request.model)
            if estimate.estimated_cost_usd > remaining_usd:
                raise AttachmentError('첨부를 포함한 추정 입력 비용이 남은 크레딧보다 큽니다. 파일 또는 범위를 줄여 주세요.', 'ATTACHMENT_BUDGET', 402)
        transaction.insert_message(self.user_message)
        for attachment in self.user_message.get('attachments', []):
            transaction.update_attachment(attachment['id'], message_id=self.user_message['id'])
        settings = dict(current["settings"])
        settings.update(
            {
                "modelId": self.request.model_id,
                "reasoningEffort": self.request.reasoning_effort,
                "thinkingEnabled": self.request.thinking_enabled,
            }
        )
        title = None
        if current["title"] == "새 대화":
            limit = CONVERSATION_TITLE_PREVIEW_LENGTH
            title_source = self.request.content or self.user_message['attachments'][0]['name']
            title = title_source[:limit] + ("…" if len(title_source) > limit else "")
        transaction.update_conversation(self.request.conversation_id, title=title, settings=settings)
        transaction.insert_model_call(
            {
                "id": self.call_id,
                "conversationId": self.request.conversation_id,
                "clientRequestId": self.request.client_request_id,
                "clientInstanceId": self.request.client_instance_id,
                "inputMessageId": self.user_message["id"],
                "outputMessageId": None,
                "request": {
                    "modelId": self.request.model_id,
                    "reasoningEffort": self.request.reasoning_effort,
                    "thinkingEnabled": self.request.thinking_enabled,
                    "webSearchEnabled": self.request.web_search_mode == "required",
                    "webSearchMode": self.request.web_search_mode,
                    "attachmentInputs": [
                        {'messageId': m['id'], 'attachmentId': a['id'], 'originalSha256': a['sha256'],
                         'preparationVersion': a['preparationVersion'], 'files': a['files']}
                        for m in context_messages for a in m.get('attachments', [])
                    ],
                },
                "status": "started",
                "startedAt": self.now,
                "completedAt": None,
            }
        )
        # Read the provider inputs inside the same transaction, so they are
        # consistent with the user message just appended.
        self.current_messages = transaction.messages(self.request.conversation_id)
        self.accumulator = StreamingGenerationAccumulator(self.current_messages, self.request.model)
        # Registration stays inside the same BEGIN IMMEDIATE window as the
        # remaining-credit snapshot. A completing call cannot settle and
        # remove the participant pool between these two operations.
        self.budget_ledger.register(
            self.request.study_id,
            self.call_id,
            usage_snapshot["limitUsd"],
            usage_snapshot["usedUsd"],
            self.accumulator.estimated_cost_usd,
        )
        self.provider_history = transaction.provider_history(
            self.request.conversation_id, self.request.model_id,
        )

    def _finish_generation(
        self,
        generation: dict,
        completion_reason: str = "completed",
        usage_accuracy: str | None = None,
    ) -> dict:
        with self.settlement_lock:
            if self.settlement["done"]:
                return self.settlement["payload"]
            accuracy = usage_accuracy or (
                "estimated" if generation.get("usageEstimated", False) else "exact"
            )
            completed_at = utc_now()
            started_at = parse_utc_timestamp(self.now)
            completed_datetime = parse_utc_timestamp(completed_at)
            duration_ms = max(
                0,
                round((completed_datetime - started_at).total_seconds() * 1000),
            ) if started_at and completed_datetime else 0
            response_parts = finalized_response_parts(
                generation.get("responseParts"), generation
            )
            citations = public_message_citations(
                generation.get("webSearchTrace"),
                generation["content"],
                self.request.model.provider,
            )
            assistant_message = {
                "id": self.assistant_id,
                "conversationId": self.request.conversation_id,
                "role": "assistant",
                "content": generation["content"],
                "reasoningSummary": generation.get("reasoningSummary", []),
                "reasoningDurationMs": duration_ms,
                "responseParts": response_parts,
                "citations": citations,
                "createdAt": completed_at,
                "completionReason": completion_reason,
                "usageAccuracy": accuracy,
            }

            with self.store.write(self.request.study_id) as transaction:
                transaction.insert_message(assistant_message)
                transaction.update_model_call(
                    self.call_id,
                    {
                        "outputMessageId": self.assistant_id,
                        "providerResult": {
                            "responseId": generation.get("providerResponseId"),
                            "status": generation.get("providerStatus"),
                            "finishReason": generation.get("finishReason"),
                        },
                        "providerState": generation.get("providerState"),
                        "usage": {
                            "inputTokens": int(generation.get("inputTokens", 0)),
                            "cachedInputTokens": int(generation.get("cachedInputTokens", 0)),
                            "cacheWriteInputTokens": int(
                                generation.get("cacheWriteInputTokens", 0)
                            ),
                            "outputTokens": int(generation.get("outputTokens", 0)),
                            "reasoningTokens": int(generation.get("reasoningTokens", 0)),
                            "webSearchRequests": int(generation.get("searchQueries", 0)),
                            "estimated": accuracy == "estimated",
                        },
                        "completionReason": completion_reason,
                        "usageAccuracy": accuracy,
                        "status": "complete",
                        "completedAt": assistant_message["createdAt"],
                    },
                )
                search_record = persisted_web_search(
                    self.call_id,
                    self.assistant_id,
                    generation.get("webSearchTrace"),
                )
                if search_record:
                    transaction.insert_web_search(search_record)
                completed_call = transaction.model_call(self.call_id)
                searches = transaction.web_searches(self.call_id)
                pricing_policy_id = transaction.meta()["pricingPolicyId"]
                final_usage = participant_usage(transaction)
            self.budget_ledger.settle(self.request.study_id, self.call_id, final_usage["usedUsd"])
            payload = {
                "userMessage": self.user_message,
                "assistantMessage": assistant_message,
                "modelCall": public_model_call(
                    completed_call, searches, pricing_policy_id
                ),
                "usage": final_usage,
            }
            self.settlement.update({"done": True, "payload": payload})
            return payload

    def _fail_generation(self, code: str) -> None:
        with self.settlement_lock:
            if self.settlement["done"]:
                return
            with self.store.write(self.request.study_id) as transaction:
                transaction.update_model_call(
                    self.call_id,
                    {"status": "failed", "errorCode": code, "completedAt": utc_now()},
                )
            self.budget_ledger.discard(self.request.study_id, self.call_id)
            self.settlement["done"] = True

    def _guarded_provider_events(self):
        try:
            resolved_messages = self.attachments.resolve(self.request.study_id, self.current_messages, self.request.model.provider)
        except Exception as exc:
            raise ModelError('ATTACHMENT_UNAVAILABLE') from exc
        self.stream_state["provider_started"] = True
        provider_stream = self.stream_response(
            resolved_messages,
            self.request.model_id,
            self.request.web_search_mode,
            self.request.reasoning_effort,
            self.request.thinking_enabled,
            self.provider_history,
            self.request.conversation_id,
        )
        try:
            for provider_event in provider_stream:
                self.accumulator.observe(provider_event)
                exhausted = False
                if provider_event.get("type") != "done":
                    exhausted = self.budget_ledger.set_estimated_cost(
                        self.request.study_id,
                        self.call_id,
                        self.accumulator.estimated_cost_usd,
                    )
                yield provider_event, exhausted
                if exhausted:
                    return
        finally:
            close_stream = getattr(provider_stream, "close", None)
            if callable(close_stream):
                close_stream()

    def disconnect(self, budget_exhausted: bool = False) -> None:
        if self.settlement["done"]:
            return
        if not self.stream_state["provider_started"]:
            # The request never reached a provider, so there is no model
            # work to charge and no partial assistant output to preserve.
            self._fail_generation("CLIENT_DISCONNECTED")
            return
        reason = "credit_exhausted" if budget_exhausted else "client_disconnected"
        self._finish_generation(
            self.accumulator.partial_generation(reason),
            reason,
            "estimated",
        )
