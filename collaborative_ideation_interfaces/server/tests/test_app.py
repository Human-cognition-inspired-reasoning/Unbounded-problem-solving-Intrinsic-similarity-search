from datetime import datetime, timezone
import json
import os
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from api.application import create_app
from models.catalog import ModelError, calculate_cost, get_model, random_initial_settings
from persistence.participant_schema import (
    ParticipantDataError,
    StorageError,
    validate_participant_state,
)
from tests.fakes import fake_generation, fake_stream_response


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-openai-key",
                "ANTHROPIC_API_KEY": "test-anthropic-key",
                "GOOGLE_API_KEY": "test-google-key",
            },
        )
        self.env_patcher.start()
        self.stream_patcher = patch("api.routes.stream_response", side_effect=fake_stream_response)
        self.stream_mock = self.stream_patcher.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.temp_dir.name,
                "SECRET_KEY": "test-secret",
                "SEED_DEMO_ACCOUNT": True,
                "DEFAULT_CREDIT_LIMIT_USD": 10.0,
                "MAX_CONTENT_LENGTH": 1024 * 1024,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()
        self.stream_patcher.stop()
        self.env_patcher.stop()

    def login(self):
        return self.client.post("/api/auth/login", json={"studyId": "P-001", "password": "prototype"})

    @staticmethod
    def parse_sse(response):
        events = []
        for frame in response.get_data(as_text=True).strip().split("\n\n"):
            lines = frame.splitlines()
            event_type = next(line[6:].strip() for line in lines if line.startswith("event:"))
            data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
            events.append((event_type, json.loads(data)))
        return events

    def test_login_required_and_login(self):
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["participant"])
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").get_json()["participant"]["studyId"], "P-001")

    def test_missing_secret_uses_an_unpredictable_ephemeral_key(self):
        keys = []
        with patch.dict(os.environ, {"FLASK_SECRET_KEY": ""}):
            for _ in range(2):
                with tempfile.TemporaryDirectory() as data_dir:
                    app = create_app(
                        {
                            "TESTING": True,
                            "DATA_DIR": data_dir,
                            "SEED_DEMO_ACCOUNT": False,
                        }
                    )
                    keys.append(app.config["SECRET_KEY"])
                    app.extensions["participant_store"].close()
        self.assertNotEqual(keys[0], "development-only-secret")
        self.assertNotEqual(keys[0], keys[1])

    def test_health_does_not_disclose_demo_or_provider_configuration(self):
        self.assertEqual(self.client.get("/api/health").get_json(), {"ok": True})

    def test_inactive_account_invalidates_an_existing_session(self):
        self.assertEqual(self.login().status_code, 200)
        store = self.app.extensions["participant_store"]
        with store._connection():
            store._connection().execute(
                "UPDATE accounts SET active = 0 WHERE study_id = ?", ("P-001",)
            )
        response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["participant"])

    def test_login_failures_are_rate_limited_and_success_clears_them(self):
        self.app.config.update(LOGIN_MAX_ATTEMPTS=2, LOGIN_WINDOW_SECONDS=900)
        wrong = {"studyId": "P-001", "password": "not-the-password"}
        self.assertEqual(self.client.post("/api/auth/login", json=wrong).status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        self.client.post("/api/auth/logout")

        self.assertEqual(self.client.post("/api/auth/login", json=wrong).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json=wrong).status_code, 401)
        limited = self.client.post("/api/auth/login", json=wrong)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["error"]["code"], "LOGIN_RATE_LIMITED")

    def test_unknown_login_ids_do_not_grow_the_throttle_table(self):
        for index in range(10):
            self.client.post(
                "/api/auth/login",
                json={"studyId": f"P-X{index:03d}", "password": "wrong-password"},
            )
        store = self.app.extensions["participant_store"]
        rows = store._connection().execute("SELECT COUNT(*) FROM login_throttle").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_default_deployment_cookie_is_secure_without_demo_mode(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app = create_app(
                {
                    "TESTING": True,
                    "DATA_DIR": data_dir,
                    "SEED_DEMO_ACCOUNT": False,
                    "SESSION_COOKIE_SECURE": True,
                }
            )
            app.extensions["participant_store"].ensure_account("P-901", "secure-password")
            response = app.test_client().post(
                "/api/auth/login",
                json={"studyId": "P-901", "password": "secure-password"},
            )
            self.assertIn("Secure", response.headers["Set-Cookie"])
            app.extensions["participant_store"].close()

    def test_conversation_and_message_storage_quotas_are_enforced(self):
        self.login()
        self.app.config.update(
            MAX_CONVERSATIONS_PER_PARTICIPANT=1,
            MAX_STORED_MESSAGE_CHARS_PER_PARTICIPANT=3,
        )
        created = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        )
        self.assertEqual(created.status_code, 201)
        second = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "CONVERSATION_LIMIT_EXCEEDED")

        conversation_id = created.get_json()["conversation"]["id"]
        oversized = self.client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={
                "content": "four",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(oversized.status_code, 409)
        self.assertEqual(
            oversized.get_json()["error"]["code"],
            "PARTICIPANT_STORAGE_LIMIT_EXCEEDED",
        )
        store = self.app.extensions["participant_store"]
        self.assertEqual(store.read("P-001").stored_message_char_count(), 0)

    def test_model_reasoning_options_match_provider_capabilities(self):
        self.login()
        models = {row["id"]: row for row in self.client.get("/api/models").get_json()["models"]}
        self.assertTrue(all(row["executionMode"] == "live" for row in models.values()))
        self.assertEqual(
            models["gpt-5.6-sol"]["reasoningOptions"],
            ["none", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            models["gpt-6-astra"]["reasoningOptions"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(models["gpt-5.6-luna"]["apiModel"], "gpt-5.6-luna")
        self.assertEqual(
            models["gpt-5.6-luna"]["reasoningOptions"],
            ["none", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            models["claude-sonnet-5"]["reasoningOptions"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(models["claude-fable-5-1"]["apiModel"], "claude-fable-5-1")
        self.assertFalse(models["claude-fable-5-1"]["supportsForcedWebSearch"])
        self.assertEqual(
            models["claude-opus-4-8"]["reasoningOptions"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(models["claude-haiku-4-5-20251001"]["apiModel"], "claude-haiku-4-5-20251001")
        self.assertEqual(models["claude-haiku-4-5-20251001"]["reasoningParameter"], "none")
        self.assertEqual(models["claude-haiku-4-5-20251001"]["reasoningOptions"], ["none"])
        self.assertTrue(models["claude-haiku-4-5-20251001"]["supportsThinkingToggle"])
        self.assertEqual(models["gemini-3.7-flash"]["label"], "Gemini 3.7 Flash")
        self.assertEqual(models["gemini-3.7-flash"]["reasoningOptions"], ["low", "medium", "high"])
        self.assertEqual(models["gemini-3.8-flash"]["label"], "Gemini 3.8 Flash")
        self.assertFalse(models["gemini-3.8-flash"]["supportsForcedWebSearch"])
        self.assertFalse(models["gemini-3.5-flash"]["selectable"])
        self.assertEqual(models["gemini-3.1-pro-preview"]["label"], "Gemini 3.1 Pro")

    def test_costly_models_leave_the_picker(self):
        self.login()
        models = {row["id"]: row for row in self.client.get("/api/models").get_json()["models"]}
        self.assertFalse(models["gpt-6-astra"]["selectable"])
        self.assertFalse(models["claude-fable-5-1"]["selectable"])

    def test_models_carry_picker_copy_and_generation_limit(self):
        self.login()
        payload = self.client.get("/api/models").get_json()
        self.assertTrue(all(row["description"] for row in payload["models"]))
        self.assertEqual(
            payload["maxConcurrentGenerations"],
            self.app.config["MAX_CONCURRENT_MODEL_CALLS_PER_PARTICIPANT"],
        )

    def test_default_conversation_uses_current_sol(self):
        self.login()
        response = self.client.post("/api/conversations", json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["conversation"]["modelId"], "gpt-6.1-sol")
        self.assertEqual(response.get_json()["conversation"]["reasoningEffort"], "medium")

    def test_new_catalog_models_settle_and_reload_with_their_own_prices(self):
        self.login()
        for model_id in (
            "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
            "claude-sonnet-5-5", "claude-opus-5-5", "gemini-3.5-flash-lite",
        ):
            with self.subTest(model=model_id):
                conversation = self.client.post(
                    "/api/conversations", json={"modelId": model_id},
                ).get_json()["conversation"]
                response = self.client.post(
                    f"/api/conversations/{conversation['id']}/messages",
                    json={"content": "카탈로그 검증", "modelId": model_id},
                )
                self.assertEqual(response.status_code, 200)
                call = response.get_json()["modelCall"]
                model = get_model(model_id)
                self.assertEqual(call["modelId"], model_id)
                self.assertEqual(call["reasoningEffort"], model.default_reasoning)
                self.assertEqual(call["pricingSnapshot"], model.pricing_snapshot(100))
                persisted = self.app.extensions["participant_store"].read("P-001").model_call(call["id"])
                self.assertEqual(persisted["request"]["modelId"], model_id)
                self.assertEqual(persisted["status"], "complete")
        self.assertAlmostEqual(self.client.get("/api/usage").get_json()["usedUsd"], sum(
            calculate_cost(get_model(model_id), 100, 0, 100, 20, 0)
            for model_id in (
                "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
                "claude-sonnet-5-5", "claude-opus-5-5", "gemini-3.5-flash-lite",
            )
        ))

    def test_claude_55_required_search_is_rejected_before_persisting_or_calling_provider(self):
        self.login()
        for model_id in ("claude-sonnet-5-5", "claude-opus-5-5"):
            conversation = self.client.post(
                "/api/conversations", json={"modelId": model_id},
            ).get_json()["conversation"]
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={"content": "Search", "modelId": model_id, "webSearchMode": "required"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"]["code"], "MODEL_SETTING_COMBINATION_UNSUPPORTED")
            self.assertEqual(self.client.get(
                f"/api/conversations/{conversation['id']}/messages",
            ).get_json()["messages"], [])
        self.stream_mock.assert_not_called()

    def test_retired_initial_assignment_is_not_rewritten_by_catalog_update(self):
        self.login()
        assignment = {
            "modelId": "gpt-5.6-sol", "reasoningEffort": "none", "thinkingEnabled": False,
            "assignedAt": "2026-09-01T00:00:00Z",
        }
        with self.app.extensions["participant_store"].write("P-001") as transaction:
            transaction.insert_initial_model_assignment(assignment)
        self.assertEqual(self.client.get("/api/models").get_json()["initialSettings"], assignment)

    def test_random_initial_settings_draw_only_selectable_models_and_their_efforts(self):
        rng = random.Random(0)
        draws = [random_initial_settings(rng) for _ in range(600)]
        for draw in draws:
            model = get_model(draw["modelId"])
            self.assertTrue(model.selectable)
            self.assertIn(draw["reasoningEffort"], model.reasoning_options)
        providers = [get_model(draw["modelId"]).provider for draw in draws]
        # Provider is drawn first, so each gets about a third regardless of
        # how many models or efforts it offers.
        for provider in ("openai", "anthropic", "google"):
            self.assertGreater(providers.count(provider), 150)
        self.assertNotIn("gpt-6-astra", {draw["modelId"] for draw in draws})
        self.assertNotIn("claude-fable-5-1", {draw["modelId"] for draw in draws})

    def test_new_participant_gets_one_stable_initial_assignment(self):
        self.login()
        first = self.client.get("/api/models").get_json()["initialSettings"]
        model = get_model(first["modelId"])
        self.assertTrue(model.selectable)
        self.assertIn(first["reasoningEffort"], model.reasoning_options)
        self.assertEqual(first["thinkingEnabled"], model.default_thinking_enabled)

        self.client.post("/api/auth/logout")
        self.login()
        self.assertEqual(self.client.get("/api/models").get_json()["initialSettings"], first)

        # Chatting afterwards does not redraw or drop the assignment.
        self.client.post("/api/conversations", json={"modelId": "gpt-5.6-luna"})
        self.assertEqual(self.client.get("/api/models").get_json()["initialSettings"], first)

    def test_participant_who_already_chatted_gets_no_initial_assignment(self):
        self.login()
        store = self.app.extensions["participant_store"]
        with store.write("P-001") as transaction:
            transaction.insert_conversation(
                {
                    "id": str(uuid.uuid4()),
                    "title": "기존 대화",
                    "settings": {
                        "modelId": "gpt-5.6-sol",
                        "reasoningEffort": "medium",
                        "thinkingEnabled": False,
                    },
                    "createdAt": "2026-09-01T00:00:00Z",
                }
            )
        self.assertIsNone(self.client.get("/api/models").get_json()["initialSettings"])
        self.assertIsNone(store.read("P-001").initial_model_assignment())

    def test_conversation_message_usage_flow(self):
        self.login()
        content = "새로운 HCI 연구 아이디어를 제안해줘. 특히 협업 과정을 포함해줘."
        created = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol", "reasoningEffort": "high"}
        )
        self.assertEqual(created.status_code, 201)
        conversation_id = created.get_json()["conversation"]["id"]
        response = self.client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={
                "content": content,
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "webSearchEnabled": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreater(payload["modelCall"]["costUsd"], 0)
        self.assertEqual(
            payload["modelCall"]["researchCreditCostUsd"],
            payload["modelCall"]["costUsd"],
        )
        self.assertTrue(payload["modelCall"]["webSearchUsed"])
        self.assertEqual(payload["modelCall"]["reasoningEffort"], "high")
        self.assertGreater(payload["modelCall"]["reasoningTokens"], 0)
        self.assertEqual(payload["modelCall"]["responseMode"], "live")
        conversation = self.client.get("/api/conversations").get_json()["conversations"][0]
        self.assertNotIn("messages", conversation)
        self.assertFalse(conversation["isGenerating"])
        self.assertEqual(conversation["messageCount"], 2)
        self.assertEqual(conversation["title"], f"{content[:20]}…")
        messages = self.client.get(
            f"/api/conversations/{conversation['id']}/messages"
        ).get_json()["messages"]
        self.assertEqual(len(messages), 2)

    def test_cache_write_tokens_survive_storage_and_reprice_the_credit_total(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "claude-sonnet-5"}
        ).get_json()["conversation"]

        def cached_generation(*args, **kwargs):
            generation = fake_generation(*args, **kwargs)
            generation["cacheWriteInputTokens"] = 40
            return generation

        def cached_stream(*args, **kwargs):
            yield {"type": "done", "generation": cached_generation(*args, **kwargs)}

        with patch("api.routes.stream_response", side_effect=cached_stream):
            payload = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "캐시 기록이 남는 요청",
                    "modelId": "claude-sonnet-5",
                    "reasoningEffort": "high",
                    "webSearchEnabled": False,
                },
            ).get_json()

        model = get_model("claude-sonnet-5")
        self.assertEqual(payload["modelCall"]["cacheWriteInputTokens"], 40)
        # The credit total is recomputed from stored usage, so the 1.25x premium
        # has to survive the round trip rather than only existing in-process.
        self.assertAlmostEqual(
            payload["modelCall"]["costUsd"],
            calculate_cost(model, 100, 0, 100, 20, 0, cache_write_input_tokens=40),
        )
        self.assertAlmostEqual(
            payload["usage"]["usedUsd"],
            payload["modelCall"]["costUsd"],
        )

    def test_current_format_normalizes_messages_calls_and_search_results(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations",
            json={"modelId": "gpt-5.6-sol", "reasoningEffort": "high"},
        ).get_json()["conversation"]

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "검색해서 알려줘",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "webSearchEnabled": True,
            },
        )
        self.assertEqual(response.status_code, 200)

        state = self.app.extensions["participant_store"].get_participant("P-001")
        self.assertEqual(state["schemaVersion"], 2)
        stored_conversation = state["conversations"][0]
        self.assertEqual(stored_conversation["settings"]["modelId"], "gpt-5.6-sol")
        self.assertNotIn("messages", stored_conversation)
        self.assertNotIn("updatedAt", stored_conversation)

        self.assertEqual([message["role"] for message in state["messages"]], ["user", "assistant"])
        call = state["modelCalls"][0]
        self.assertEqual(call["inputMessageId"], state["messages"][0]["id"])
        self.assertEqual(call["outputMessageId"], state["messages"][1]["id"])
        self.assertEqual(call["request"]["reasoningEffort"], "high")
        self.assertEqual(call["usage"]["webSearchRequests"], 1)
        for derived_key in ("costUsd", "researchCreditCostUsd", "pricingSnapshot", "webSearchUsed"):
            self.assertNotIn(derived_key, call)

        search = state["webSearches"][0]
        self.assertEqual(search["modelCallId"], call["id"])
        self.assertEqual(search["executions"][0]["queries"], ["test query"])
        self.assertEqual(search["sources"][0]["url"], "https://example.com/test")
        self.assertEqual(search["citations"][0]["messageId"], state["messages"][1]["id"])
        self.assertEqual(search["citations"][0]["sourceId"], search["sources"][0]["id"])
        validate_participant_state(state)

    def test_conversation_list_omits_message_bodies(self):
        self.login()
        created = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        self.assertEqual(created["messages"], [])

        rows = self.client.get("/api/conversations").get_json()["conversations"]
        listed = next(row for row in rows if row["id"] == created["id"])
        # The list carries a count, never the bodies: that response is on every
        # page load, and 100 participants log in at once.
        self.assertNotIn("messages", listed)
        self.assertEqual(listed["messageCount"], 0)

        messages = self.client.get(
            f"/api/conversations/{created['id']}/messages"
        ).get_json()["messages"]
        self.assertEqual(messages, [])

        missing = self.client.get("/api/conversations/does-not-exist/messages")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"]["code"], "CONVERSATION_NOT_FOUND")

    def test_conversation_messages_endpoint_requires_login(self):
        response = self.client.get("/api/conversations/any/messages")
        self.assertEqual(response.status_code, 401)

    def test_unsupported_participant_version_is_rejected(self):
        store = self.app.extensions["participant_store"]

        with self.assertRaises(ParticipantDataError):
            validate_participant_state({"schemaVersion": 1, "participantId": "P-002"})

        # Whole-document reads still reject an unsupported stored version.
        with store.write("P-001") as transaction:
            transaction._connection.execute(
                "UPDATE participants SET schema_version = 1 WHERE study_id = 'P-001'"
            )
        with self.assertRaises(StorageError):
            store.get_participant("P-001")

    def test_message_longer_than_8000_characters_is_accepted(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        content = "a" * 9_000

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": content,
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["userMessage"]["content"], content)

    def test_json_request_larger_than_one_megabyte_is_rejected_as_json(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "a" * (1024 * 1024),
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"]["code"], "REQUEST_TOO_LARGE")

    def test_context_limit_error_tells_user_to_start_a_new_conversation(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]

        with patch("api.routes.stream_response", side_effect=ModelError("CONTEXT_LIMIT_EXCEEDED")):
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "컨텍스트 한도를 넘기는 메시지",
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": "medium",
                    "webSearchEnabled": False,
                },
                headers={"Accept": "text/event-stream"},
                buffered=True,
            )

        events = self.parse_sse(response)
        error_event = events[-1]
        self.assertEqual(error_event[0], "error")
        self.assertEqual(error_event[1]["status"], 422)
        self.assertEqual(error_event[1]["error"]["code"], "CONTEXT_LIMIT_EXCEEDED")
        self.assertIn("새 대화", error_event[1]["error"]["message"])

    def test_only_one_simultaneous_call_can_start_in_the_same_conversation(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        first_client = self.app.test_client()
        second_client = self.app.test_client()
        for client in (first_client, second_client):
            login = client.post("/api/auth/login", json={"studyId": "P-001", "password": "prototype"})
            self.assertEqual(login.status_code, 200)

        provider_started = threading.Event()
        release_provider = threading.Event()
        first_result = {}

        def blocking_stream(*args, **kwargs):
            provider_started.set()
            if not release_provider.wait(timeout=5):
                raise AssertionError("test provider was not released")
            yield from fake_stream_response(*args, **kwargs)

        def send_first_message():
            first_result["response"] = first_client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "첫 번째 탭의 메시지",
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": "medium",
                    "webSearchEnabled": False,
                },
                headers={"Accept": "text/event-stream"},
                buffered=True,
            )

        with patch("api.routes.stream_response", side_effect=blocking_stream):
            first_thread = threading.Thread(target=send_first_message)
            first_thread.start()
            self.assertTrue(provider_started.wait(timeout=2))

            second = second_client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "두 번째 탭의 동시 메시지",
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": "medium",
                    "webSearchEnabled": False,
                },
            )
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.get_json()["error"]["code"], "CONVERSATION_BUSY")
            listed = second_client.get("/api/conversations").get_json()["conversations"]
            listed_conversation = next(row for row in listed if row["id"] == conversation["id"])
            self.assertTrue(listed_conversation["isGenerating"])

            state_while_busy = self.app.extensions["participant_store"].get_participant("P-001")
            stored_messages = [
                message
                for message in state_while_busy["messages"]
                if message["conversationId"] == conversation["id"]
            ]
            self.assertEqual([message["content"] for message in stored_messages], ["첫 번째 탭의 메시지"])
            self.assertEqual(len(state_while_busy["modelCalls"]), 1)
            self.assertEqual(state_while_busy["modelCalls"][0]["status"], "started")

            release_provider.set()
            first_thread.join(timeout=5)

        self.assertFalse(first_thread.is_alive())
        self.assertEqual(first_result["response"].status_code, 200)
        final_state = self.app.extensions["participant_store"].get_participant("P-001")
        final_messages = [
            message
            for message in final_state["messages"]
            if message["conversationId"] == conversation["id"]
        ]
        self.assertEqual(len(final_messages), 2)
        self.assertNotIn("두 번째 탭의 동시 메시지", [row["content"] for row in final_messages])
        self.assertEqual(len(final_state["modelCalls"]), 1)
        self.assertEqual(final_state["modelCalls"][0]["status"], "complete")

    def test_started_call_in_another_conversation_does_not_block_sending(self):
        self.login()
        conversations = [
            self.client.post(
                "/api/conversations", json={"modelId": "gpt-5.6-sol"}
            ).get_json()["conversation"]
            for _ in range(2)
        ]
        store = self.app.extensions["participant_store"]

        def seed_active_call(state):
            state["modelCalls"].append(
                {
                    "id": "active-other-conversation",
                    "conversationId": conversations[0]["id"],
                    "clientRequestId": "active-other-request",
                    "clientInstanceId": "other-tab",
                    "inputMessageId": None,
                    "outputMessageId": None,
                    "request": {
                        "modelId": "gpt-5.6-sol",
                        "reasoningEffort": "medium",
                        "thinkingEnabled": False,
                        "webSearchEnabled": False,
                    },
                    "status": "started",
                    "startedAt": "2099-01-01T00:00:00Z",
                    "completedAt": None,
                }
            )

        store.update_participant("P-001", seed_active_call)
        response = self.client.post(
            f"/api/conversations/{conversations[1]['id']}/messages",
            json={
                "content": "다른 대화의 동시 메시지",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        state = store.get_participant("P-001")
        self.assertEqual(state["modelCalls"][0]["status"], "started")
        self.assertEqual(state["modelCalls"][1]["status"], "complete")

    def test_participant_cannot_start_more_than_three_concurrent_calls(self):
        self.login()
        conversations = [
            self.client.post(
                "/api/conversations", json={"modelId": "gpt-5.6-sol"}
            ).get_json()["conversation"]
            for _ in range(4)
        ]
        store = self.app.extensions["participant_store"]

        def seed_active_calls(state):
            for index, conversation in enumerate(conversations[:3]):
                state["modelCalls"].append(
                    {
                        "id": f"active-call-{index}",
                        "conversationId": conversation["id"],
                        "clientRequestId": f"active-request-{index}",
                        "clientInstanceId": f"tab-{index}",
                        "inputMessageId": None,
                        "outputMessageId": None,
                        "request": {
                            "modelId": "gpt-5.6-sol",
                            "reasoningEffort": "medium",
                            "thinkingEnabled": False,
                            "webSearchEnabled": False,
                        },
                        "status": "started",
                        "startedAt": "2099-01-01T00:00:00Z",
                        "completedAt": None,
                    }
                )

        store.update_participant("P-001", seed_active_calls)
        response = self.client.post(
            f"/api/conversations/{conversations[3]['id']}/messages",
            json={
                "content": "네 번째 동시 메시지",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "CONCURRENT_CALL_LIMIT_EXCEEDED",
        )
        state = store.get_participant("P-001")
        self.assertEqual(len(state["modelCalls"]), 3)
        self.assertNotIn("네 번째 동시 메시지", [row["content"] for row in state["messages"]])

    def test_stale_started_call_does_not_lock_conversation_forever(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        store = self.app.extensions["participant_store"]

        def seed_stale_call(state):
            state["modelCalls"].append(
                {
                    "id": "stale-call",
                    "conversationId": conversation["id"],
                    "clientRequestId": None,
                    "clientInstanceId": None,
                    "inputMessageId": None,
                    "outputMessageId": None,
                    "request": {
                        "modelId": "gpt-5.6-sol",
                        "reasoningEffort": "medium",
                        "thinkingEnabled": False,
                        "webSearchEnabled": False,
                    },
                    "status": "started",
                    "startedAt": "2000-01-01T00:00:00Z",
                    "completedAt": None,
                }
            )

        store.update_participant("P-001", seed_stale_call)
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "새 요청",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        final_state = store.get_participant("P-001")
        stale_call = next(row for row in final_state["modelCalls"] if row["id"] == "stale-call")
        self.assertEqual(stale_call["status"], "failed")
        self.assertEqual(stale_call["errorCode"], "STALE_MODEL_CALL")
        self.assertIsNotNone(stale_call["completedAt"])

    def test_client_request_id_prevents_sequential_duplicate_storage(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        request_payload = {
            "content": "한 번만 저장될 요청",
            "modelId": "gpt-5.6-sol",
            "reasoningEffort": "medium",
            "webSearchEnabled": False,
            "clientRequestId": "client-request-1",
            "clientInstanceId": "client-instance-1",
        }

        first = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json=request_payload,
        )
        second = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json=request_payload,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "DUPLICATE_REQUEST")
        state = self.app.extensions["participant_store"].get_participant("P-001")
        self.assertEqual(len(state["modelCalls"]), 1)
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["modelCalls"][0]["clientRequestId"], "client-request-1")
        self.assertEqual(state["modelCalls"][0]["clientInstanceId"], "client-instance-1")

    def test_conversation_title_is_automatic_and_delete_is_not_available(self):
        self.login()
        created = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol", "reasoningEffort": "medium"}
        )
        conversation = created.get_json()["conversation"]

        renamed = self.client.patch(
            f"/api/conversations/{conversation['id']}", json={"title": "사용자 지정 제목"}
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.get_json()["conversation"]["title"], "새 대화")
        self.assertEqual(self.client.delete(f"/api/conversations/{conversation['id']}").status_code, 405)

    def test_message_stream_emits_deltas_then_persists_final_response(self):
        self.login()
        created = self.client.post(
            "/api/conversations", json={"modelId": "claude-sonnet-5", "reasoningEffort": "high"}
        )
        conversation_id = created.get_json()["conversation"]["id"]
        response = self.client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={
                "content": "스트리밍으로 연구 아이디어를 보여줘",
                "modelId": "claude-sonnet-5",
                "reasoningEffort": "high",
                "thinkingEnabled": True,
                "webSearchEnabled": False,
            },
            headers={"Accept": "text/event-stream"},
            buffered=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("text/event-stream"))
        events = self.parse_sse(response)
        self.assertEqual(self.stream_mock.call_args.args[2], "auto")
        self.assertEqual(events[0][0], "message_start")
        self.assertEqual(events[-1][0], "message_done")
        self.assertIn("reasoning_delta", [event_type for event_type, _ in events])
        self.assertIn("status", [event_type for event_type, _ in events])
        deltas = [payload["delta"] for event_type, payload in events if event_type == "text_delta"]
        self.assertGreater(len(deltas), 1)
        completion = events[-1][1]
        self.assertEqual("".join(deltas), completion["assistantMessage"]["content"])
        self.assertGreater(completion["modelCall"]["costUsd"], 0)
        self.assertEqual(
            completion["assistantMessage"]["reasoningSummary"][0]["text"],
            "질문을 분석하고 답변을 구성했습니다.",
        )
        self.assertEqual(
            [part["type"] for part in completion["assistantMessage"]["responseParts"]],
            ["reasoning", "text", "reasoning", "text"],
        )
        self.assertNotIn("providerState", completion["modelCall"])
        conversation = self.client.get("/api/conversations").get_json()["conversations"][0]
        messages = self.client.get(
            f"/api/conversations/{conversation['id']}/messages"
        ).get_json()["messages"]
        self.assertEqual(messages[-1], completion["assistantMessage"])
        state = self.app.extensions["participant_store"].get_participant("P-001")
        self.assertEqual(state["modelCalls"][-1]["providerState"]["payload"]["opaque"], "server-only")

    def test_json_and_sse_share_settlement_and_closing_does_not_charge_twice(self):
        self.login()
        completions = []
        store = self.app.extensions["participant_store"]
        for accept in ("application/json", "text/event-stream"):
            with self.subTest(accept=accept):
                conversation = self.client.post(
                    "/api/conversations", json={"modelId": "gpt-5.6-sol"}
                ).get_json()["conversation"]
                response = self.client.post(
                    f"/api/conversations/{conversation['id']}/messages",
                    json={"content": "동일한 질문", "modelId": "gpt-5.6-sol", "reasoningEffort": "medium"},
                    headers={"Accept": accept}, buffered=True,
                )
                completion = (self.parse_sse(response)[-1][1]
                              if accept == "text/event-stream" else response.get_json())
                completions.append(completion)
                usage = self.client.get("/api/usage").get_json()
                response.close()
                response.close()
                self.assertEqual(self.client.get("/api/usage").get_json(), usage)
                self.assertEqual(len(store.read("P-001").model_calls(conversation['id'])), 1)
                self.assertEqual(len(store.read("P-001").messages(conversation['id'])), 2)
                self.assertFalse(store.read("P-001").active_model_calls())
        for field in ("content", "responseParts", "reasoningSummary", "completionReason", "usageAccuracy"):
            self.assertEqual(completions[0]['assistantMessage'][field], completions[1]['assistantMessage'][field])
        self.assertEqual(completions[0]['modelCall']['costUsd'], completions[1]['modelCall']['costUsd'])

    def test_disconnect_at_budget_boundary_keeps_credit_exhausted_reason(self):
        self.login()
        store = self.app.extensions["participant_store"]
        store.update_participant("P-001", lambda state: state.update(creditLimitUsd=0.0001))
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        closed = threading.Event()

        def expensive_stream(*_args):
            try:
                yield {"type": "text_delta", "delta": "abcdefghij" * 4}
                raise AssertionError("The provider must close at the budget boundary")
            finally:
                closed.set()

        with patch("api.routes.stream_response", side_effect=expensive_stream):
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={"content": "질문", "modelId": "gpt-5.6-sol", "reasoningEffort": "medium"},
                headers={"Accept": "text/event-stream"}, buffered=False,
            )
            chunks = iter(response.response)
            while b'event: text_delta' not in next(chunks):
                pass
            response.close()
        call = store.read("P-001").model_calls(conversation['id'])[0]
        self.assertEqual(call['completionReason'], 'credit_exhausted')
        self.assertEqual(call['usageAccuracy'], 'estimated')
        self.assertTrue(closed.is_set())

    def test_stream_cutoff_persists_visible_partial_output_and_closes_provider(self):
        self.login()
        store = self.app.extensions["participant_store"]
        store.update_participant(
            "P-001",
            lambda draft: draft.update({"creditLimitUsd": 0.0001}),
        )
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        provider_closed = threading.Event()

        def expensive_stream(*_args, **_kwargs):
            try:
                yield {"type": "text_delta", "delta": "abcdefghij" * 4}
                raise AssertionError("the stream should stop at the live credit boundary")
            finally:
                provider_closed.set()

        with patch("api.routes.stream_response", side_effect=expensive_stream):
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": "질문",
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": "medium",
                    "webSearchEnabled": False,
                },
                headers={"Accept": "text/event-stream"},
                buffered=True,
            )

        completion = self.parse_sse(response)[-1]
        self.assertEqual(completion[0], "message_done")
        self.assertEqual(
            completion[1]["assistantMessage"]["content"],
            "abcdefghij" * 4,
        )
        self.assertEqual(
            completion[1]["assistantMessage"]["completionReason"],
            "credit_exhausted",
        )
        self.assertEqual(completion[1]["modelCall"]["usageAccuracy"], "estimated")
        self.assertTrue(provider_closed.is_set())

    def test_disconnect_before_provider_is_free_but_after_delta_is_estimated(self):
        self.login()
        conversations = [
            self.client.post(
                "/api/conversations", json={"modelId": "gpt-5.6-sol"}
            ).get_json()["conversation"]
            for _ in range(2)
        ]

        def partial_stream(*args, **kwargs):
            yield {"type": "reasoning_delta", "id": "r", "index": 0, "delta": "생각 중"}
            yield from fake_stream_response(*args, **kwargs)

        request_payload = {
            "content": "질문",
            "modelId": "gpt-5.6-sol",
            "reasoningEffort": "medium",
            "webSearchEnabled": False,
        }
        with patch("api.routes.stream_response", side_effect=partial_stream) as provider:
            unopened = self.client.post(
                f"/api/conversations/{conversations[0]['id']}/messages",
                json=request_payload,
                headers={"Accept": "text/event-stream"},
                buffered=False,
            )
            unopened.close()
            self.assertEqual(provider.call_count, 0)

            started = self.client.post(
                f"/api/conversations/{conversations[1]['id']}/messages",
                json=request_payload,
                headers={"Accept": "text/event-stream"},
                buffered=False,
            )
            chunks = iter(started.response)
            for _ in range(3):
                next(chunks)
            started.close()

        state = self.app.extensions["participant_store"].get_participant("P-001")
        self.assertEqual(state["modelCalls"][0]["status"], "failed")
        self.assertEqual(state["modelCalls"][0]["errorCode"], "CLIENT_DISCONNECTED")
        self.assertNotIn("usage", state["modelCalls"][0])
        self.assertEqual(state["modelCalls"][1]["status"], "complete")
        self.assertEqual(
            state["modelCalls"][1]["completionReason"],
            "client_disconnected",
        )
        self.assertTrue(state["modelCalls"][1]["usage"]["estimated"])

    def test_message_stream_shows_sources_and_persists_search_progress(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol", "reasoningEffort": "medium"}
        ).get_json()["conversation"]

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "검색하면서 진행 상황을 보여줘",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchMode": "required",
            },
            headers={"Accept": "text/event-stream"},
            buffered=True,
        )

        events = self.parse_sse(response)
        self.assertEqual(self.stream_mock.call_args.args[2], "required")
        event_types = [event_type for event_type, _ in events]
        self.assertNotIn("search_query", event_types)
        self.assertLess(event_types.index("search_started"), event_types.index("source_added"))
        self.assertLess(event_types.index("source_added"), event_types.index("message_done"))
        source = next(payload for event_type, payload in events if event_type == "source_added")
        self.assertEqual(source["searchId"], "fake-search-0")
        self.assertEqual(source["source"]["url"], "https://example.com/test")
        call = events[-1][1]["modelCall"]
        self.assertEqual(call["webSearchMode"], "required")
        self.assertTrue(call["webSearchEnabled"])
        assistant = events[-1][1]["assistantMessage"]
        self.assertEqual(
            [part["type"] for part in assistant["responseParts"]],
            ["reasoning", "text", "search", "reasoning", "text"],
        )
        self.assertEqual(assistant["responseParts"][2]["sources"][0]["url"], "https://example.com/test")
        self.assertEqual(assistant["citations"][0]["placement"], "inline")
        conversation_id = self.client.get(
            "/api/conversations"
        ).get_json()["conversations"][0]["id"]
        persisted = self.client.get(
            f"/api/conversations/{conversation_id}/messages"
        ).get_json()["messages"][-1]
        self.assertEqual(persisted["responseParts"], assistant["responseParts"])

    def test_claude_citations_are_public_footer_sources_without_offsets(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "claude-sonnet-5", "reasoningEffort": "high"}
        ).get_json()["conversation"]
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "검색해서 인용해줘",
                "modelId": "claude-sonnet-5",
                "reasoningEffort": "high",
                "thinkingEnabled": True,
                "webSearchMode": "required",
            },
        )
        self.assertEqual(response.status_code, 200)
        citation = response.get_json()["assistantMessage"]["citations"][0]
        self.assertEqual(citation["placement"], "footer")
        self.assertEqual(citation["url"], "https://example.com/test")
        self.assertNotIn("startIndex", citation)
        self.assertNotIn("endIndex", citation)

    def test_invalid_reasoning_effort_is_rejected(self):
        self.login()
        conversation = self.client.post("/api/conversations", json={"modelId": "claude-sonnet-5"}).get_json()["conversation"]
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "test",
                "modelId": "claude-sonnet-5",
                "reasoningEffort": "minimal",
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_MODEL_SETTINGS")

    def test_haiku_requires_thinking_off_when_search_is_forced(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations",
            json={"modelId": "claude-haiku-4-5-20251001", "reasoningEffort": "none"},
        ).get_json()["conversation"]
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "반드시 검색해줘",
                "modelId": "claude-haiku-4-5-20251001",
                "reasoningEffort": "none",
                "thinkingEnabled": True,
                "webSearchMode": "required",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "MODEL_SETTING_COMBINATION_UNSUPPORTED",
        )

    def test_fable_rejects_forced_search_before_calling_the_provider(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations",
            json={"modelId": "claude-fable-5-1", "reasoningEffort": "high"},
        ).get_json()["conversation"]
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "반드시 검색해줘",
                "modelId": "claude-fable-5-1",
                "reasoningEffort": "high",
                "thinkingEnabled": True,
                "webSearchMode": "required",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "MODEL_SETTING_COMBINATION_UNSUPPORTED",
        )

    def test_retired_model_leaves_the_picker_but_still_prices_past_calls(self):
        self.login()
        models = {row["id"]: row for row in self.client.get("/api/models").get_json()["models"]}
        self.assertFalse(models["claude-opus-4-8"]["selectable"])
        self.assertFalse(models["claude-opus-5"]["selectable"])
        self.assertTrue(models["claude-opus-5-5"]["selectable"])

        # A conversation recorded against the retired model must keep working:
        # usage still prices it and the conversation still serializes.
        store = self.app.extensions["participant_store"]

        def seed(state):
            state["conversations"].append(
                {
                    "id": "retired-conversation",
                    "title": "은퇴 모델 대화",
                    "settings": {
                        "modelId": "claude-opus-4-8",
                        "reasoningEffort": "high",
                        "thinkingEnabled": True,
                    },
                    "createdAt": "2026-08-01T00:00:00Z",
                }
            )
            state["messages"].append(
                {
                    "id": "retired-user",
                    "conversationId": "retired-conversation",
                    "role": "user",
                    "content": "안녕",
                    "createdAt": "2026-08-01T00:00:01Z",
                }
            )
            state["messages"].append(
                {
                    "id": "retired-assistant",
                    "conversationId": "retired-conversation",
                    "role": "assistant",
                    "content": "반가워",
                    "createdAt": "2026-08-01T00:00:02Z",
                }
            )
            state["modelCalls"].append(
                {
                    "id": "retired-call",
                    "conversationId": "retired-conversation",
                    "clientRequestId": "retired-request",
                    "clientInstanceId": "retired-instance",
                    "inputMessageId": "retired-user",
                    "outputMessageId": "retired-assistant",
                    "request": {
                        "modelId": "claude-opus-4-8",
                        "reasoningEffort": "high",
                        "thinkingEnabled": True,
                        "webSearchEnabled": False,
                    },
                    "status": "complete",
                    "startedAt": "2026-08-01T00:00:01Z",
                    "completedAt": "2026-08-01T00:00:02Z",
                    "usage": {
                        "inputTokens": 1000,
                        "cachedInputTokens": 0,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 500,
                        "reasoningTokens": 0,
                        "webSearchRequests": 0,
                        "estimated": False,
                    },
                }
            )

        store.update_participant("P-001", seed)

        usage = self.client.get("/api/usage")
        self.assertEqual(usage.status_code, 200)
        self.assertGreater(usage.get_json()["usedUsd"], 0)

        conversations = self.client.get("/api/conversations")
        self.assertEqual(conversations.status_code, 200)
        retired = next(
            row for row in conversations.get_json()["conversations"]
            if row["id"] == "retired-conversation"
        )
        self.assertEqual(retired["modelId"], "claude-opus-4-8")

    def test_haiku_thinking_toggle_is_persisted(self):
        # Haiku has no effort parameter, so the toggle is its only depth control
        # and stays a participant-facing setting.
        self.login()
        created = self.client.post(
            "/api/conversations",
            json={"modelId": "claude-haiku-4-5-20251001", "reasoningEffort": "none"},
        )
        conversation = created.get_json()["conversation"]
        self.assertTrue(conversation["thinkingEnabled"])

        updated = self.client.patch(
            f"/api/conversations/{conversation['id']}", json={"thinkingEnabled": False}
        )
        self.assertFalse(updated.get_json()["conversation"]["thinkingEnabled"])

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "간단한 연구 질문",
                "modelId": "claude-haiku-4-5-20251001",
                "reasoningEffort": "none",
                "thinkingEnabled": False,
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["modelCall"]["thinkingEnabled"])

    def test_effort_models_have_no_thinking_toggle_and_always_think(self):
        """effort가 있는 Claude 모델은 깊이 조절을 effort 하나로 한다.

        토글과 effort는 겹치는 다이얼이고, Anthropic 권고는 thinking을 끄는 대신
        effort를 낮추는 것이다. 토글을 없애면 Opus 5가 거부하는 조합(thinking off
        + xhigh/max)도 애초에 도달할 수 없다.
        """
        self.login()
        models = {row["id"]: row for row in self.client.get("/api/models").get_json()["models"]}
        for model_id in ("claude-sonnet-5", "claude-opus-5"):
            self.assertFalse(models[model_id]["supportsThinkingToggle"], model_id)
            self.assertTrue(models[model_id]["alwaysThinks"], model_id)
            self.assertTrue(models[model_id]["defaultThinkingEnabled"], model_id)
        self.assertTrue(models["claude-haiku-4-5-20251001"]["supportsThinkingToggle"])
        self.assertFalse(models["claude-haiku-4-5-20251001"]["alwaysThinks"])

        # A stale thinkingEnabled=False from a client is ignored, and the call is
        # recorded as thinking -- because it did.
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "claude-opus-5", "reasoningEffort": "max"}
        ).get_json()["conversation"]
        self.assertTrue(conversation["thinkingEnabled"])
        response = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "가장 깊게 생각해줘",
                "modelId": "claude-opus-5",
                "reasoningEffort": "max",
                "thinkingEnabled": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["modelCall"]["thinkingEnabled"])

    def test_provider_switch_requires_new_conversation(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol"}
        ).get_json()["conversation"]
        first = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "첫 번째 메시지",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(first.status_code, 200)

        switched = self.client.patch(
            f"/api/conversations/{conversation['id']}",
            json={"modelId": "claude-sonnet-5", "reasoningEffort": "high"},
        )
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(
            switched.get_json()["error"]["code"],
            "PROVIDER_SWITCH_REQUIRES_NEW_CONVERSATION",
        )

    def test_private_provider_state_reuse_requires_exact_model_but_allows_effort_change(self):
        self.login()
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol", "reasoningEffort": "low"}
        ).get_json()["conversation"]

        for effort in ("low", "high"):
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={
                    "content": f"{effort} 단계 질문",
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": effort,
                    "webSearchEnabled": False,
                },
            )
            self.assertEqual(response.status_code, 200)

        second_history = self.stream_mock.call_args_list[1].args[5]
        self.assertEqual(len(second_history), 1)
        self.assertEqual(second_history[0]["modelId"], "gpt-5.6-sol")

        switched = self.client.patch(
            f"/api/conversations/{conversation['id']}",
            json={"modelId": "gpt-5.6-terra", "reasoningEffort": "medium"},
        )
        self.assertEqual(switched.status_code, 200)
        third = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "모델을 바꾼 질문",
                "modelId": "gpt-5.6-terra",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(third.status_code, 200)
        self.assertEqual(self.stream_mock.call_args_list[2].args[5], [])

    def test_call_that_crosses_credit_limit_is_saved_then_future_calls_are_blocked(self):
        self.login()
        store = self.app.extensions["participant_store"]
        store.update_participant("P-001", lambda draft: draft.update({"creditLimitUsd": 0.0011}))
        conversation = self.client.post(
            "/api/conversations", json={"modelId": "gpt-5.6-sol", "reasoningEffort": "medium"}
        ).get_json()["conversation"]

        first = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "검색을 사용해 한도를 넘는 응답을 만들어줘",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": True,
            },
        )
        self.assertEqual(first.status_code, 200)
        self.assertGreater(first.get_json()["usage"]["usedUsd"], 0.0011)
        self.assertEqual(first.get_json()["usage"]["remainingUsd"], 0)

        second = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "content": "다음 요청",
                "modelId": "gpt-5.6-sol",
                "reasoningEffort": "medium",
                "webSearchEnabled": False,
            },
        )
        self.assertEqual(second.status_code, 402)


if __name__ == "__main__":
    unittest.main()
