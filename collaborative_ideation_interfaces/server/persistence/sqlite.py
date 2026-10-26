"""SQLite participant store for schema version 2 data.

Writes go through `store.write(study_id)`, a `BEGIN IMMEDIATE` transaction
exposing targeted queries. Callers keep their own business rules; the store
only owns persistence and the invariants that are cheaper as indexes
(see schema.sql).

`validate_participant_state` from `participant_schema.py` is deliberately not on the
request path. It validates whole-participant reads and updates for tests
and offline inspection.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

from werkzeug.security import check_password_hash, generate_password_hash

from persistence.participant_schema import (
    DEFAULT_PRICING_POLICY_ID,
    PARTICIPANT_DATA_VERSION,
    ParticipantDataError,
    StorageError,
    validate_participant_state,
    validate_study_id,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# How a JSON row maps onto columns.
#
# "required" keys are always present and never null. "nullable" keys are always
# present but may hold null. "optional" keys are absent from the JSON row when
# they do not apply, which is how the v2 data distinguishes "not an assistant
# message" from "an empty list" -- so NULL in those columns means "omit the
# key", not "emit null".
_MESSAGE_REQUIRED = (("id", "id"), ("conversationId", "conversation_id"),
                     ("role", "role"), ("content", "content"),
                     ("createdAt", "created_at"))
_MESSAGE_OPTIONAL_JSON = (("reasoningSummary", "reasoning_summary"),
                          ("responseParts", "response_parts"),
                          ("citations", "citations"))
_MESSAGE_OPTIONAL_SCALAR = (("reasoningDurationMs", "reasoning_duration_ms"),)

_CALL_REQUIRED = (("id", "id"), ("conversationId", "conversation_id"),
                  ("status", "status"), ("startedAt", "started_at"))
_CALL_NULLABLE = (("clientRequestId", "client_request_id"),
                  ("clientInstanceId", "client_instance_id"),
                  ("inputMessageId", "input_message_id"),
                  ("outputMessageId", "output_message_id"),
                  ("completedAt", "completed_at"))
_CALL_OPTIONAL_JSON = (("usage", "usage"), ("providerResult", "provider_result"))
_CALL_OPTIONAL_SCALAR = (("errorCode", "error_code"),)

_MESSAGE_KEYS = {key for key, _ in _MESSAGE_REQUIRED + _MESSAGE_OPTIONAL_JSON
                 + _MESSAGE_OPTIONAL_SCALAR}
_CALL_KEYS = ({key for key, _ in _CALL_REQUIRED + _CALL_NULLABLE
               + _CALL_OPTIONAL_JSON + _CALL_OPTIONAL_SCALAR}
              | {"request", "providerState"})
_CONVERSATION_KEYS = {"id", "title", "settings", "createdAt"}
_PROVIDER_STATE_KEYS = {"schemaVersion", "provider", "modelId", "payload"}


def _dump(value: Any) -> str | None:
    """Serialise a JSON leaf. None means "the key was absent"."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _extras(row: dict, known: set[str]) -> str | None:
    """Capture keys this schema does not map, so a dump stays lossless."""
    rest = {key: value for key, value in row.items() if key not in known}
    return json.dumps(rest, ensure_ascii=False) if rest else None


def _merge_extras(target: dict, raw: str | None) -> dict:
    if raw:
        target.update(json.loads(raw))
    return target


class SqliteStore:
    """Participant persistence backed by one SQLite database."""

    def __init__(self, db_path: str | Path, default_credit_limit: float = 10.0):
        self.db_path = Path(db_path)
        self.default_credit_limit = float(default_credit_limit)
        self._local = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    # ------------------------------------------------------------ connection

    def _initialise(self) -> None:
        connection = self._connection()
        with connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('participantDataVersion', ?)",
                (str(PARTICIPANT_DATA_VERSION),),
            )

    def _connection(self) -> sqlite3.Connection:
        """One connection per thread. sqlite3 connections are not thread-safe."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        try:
            connection = sqlite3.connect(self.db_path, timeout=30.0)
        except sqlite3.Error as exc:  # pragma: no cover - unreachable in tests
            raise StorageError(f"Could not open {self.db_path.name}") from exc
        connection.row_factory = sqlite3.Row
        # WAL lets readers run while the single writer commits. synchronous
        # NORMAL is durable against process crashes under WAL and avoids an
        # fsync per commit, which is what a small Persistent Disk cannot afford.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        self._local.connection = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    @contextmanager
    def write(self, study_id: str) -> Iterator["ParticipantTx"]:
        """Open a write transaction.

        `BEGIN IMMEDIATE` takes the write lock up front. Starting deferred and
        upgrading mid-transaction is the classic SQLite deadlock: two readers
        that both want to write get SQLITE_BUSY on upgrade instead of queueing.
        """
        study_id = self.validate_study_id(study_id)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise StorageError("Could not begin a participant write") from exc
        transaction = ParticipantTx(connection, study_id)
        try:
            yield transaction
        except BaseException:
            connection.rollback()
            raise
        connection.commit()

    def read(self, study_id: str) -> "ParticipantTx":
        return ParticipantTx(self._connection(), self.validate_study_id(study_id))

    # --------------------------------------------------------------- accounts

    validate_study_id = staticmethod(validate_study_id)

    def ensure_account(
        self, study_id: str, password: str, credit_limit: float | None = None
    ) -> None:
        study_id = self.validate_study_id(study_id)
        if len(password) < 8:
            raise ValueError("Password must contain at least 8 characters")
        limit = float(credit_limit if credit_limit is not None else self.default_credit_limit)
        connection = self._connection()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM accounts WHERE study_id = ?", (study_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO accounts(study_id, password_hash, active, credit_limit_usd)"
                    " VALUES(?, ?, 1, ?)",
                    (study_id, generate_password_hash(password), limit),
                )
            self._ensure_participant(connection, study_id, limit)

    def authenticate(self, study_id: str, password: str) -> bool:
        try:
            study_id = self.validate_study_id(study_id)
        except ValueError:
            return False
        row = self._connection().execute(
            "SELECT password_hash, active FROM accounts WHERE study_id = ?", (study_id,)
        ).fetchone()
        return bool(row and row["active"] and check_password_hash(row["password_hash"], password))

    def list_accounts(self) -> list[sqlite3.Row]:
        """Return account status and record counts for administration."""
        return self._connection().execute(
            "SELECT a.study_id, a.active, a.credit_limit_usd,"
            "       (SELECT COUNT(*) FROM conversations c WHERE c.study_id = a.study_id) AS conversations,"
            "       (SELECT COUNT(*) FROM messages m WHERE m.study_id = a.study_id) AS messages"
            "  FROM accounts a ORDER BY a.study_id"
        ).fetchall()

    def account_is_active(self, study_id: str) -> bool:
        try:
            study_id = self.validate_study_id(study_id)
        except ValueError:
            return False
        row = self._connection().execute(
            "SELECT active FROM accounts WHERE study_id = ?", (study_id,)
        ).fetchone()
        return bool(row and row["active"])

    def authenticate_rate_limited(
        self,
        study_id: str,
        password: str,
        *,
        max_attempts: int,
        window_seconds: int,
        now: float | None = None,
    ) -> str:
        """Authenticate with a deployment-wide fixed window per real account.

        Unknown IDs intentionally create no throttle row: otherwise a public
        endpoint could grow the database with arbitrary bucket names. Their
        lookup also avoids the expensive password hash operation.
        """
        try:
            study_id = self.validate_study_id(study_id)
        except ValueError:
            return "invalid"
        current_time = time.time() if now is None else float(now)
        max_attempts = max(1, int(max_attempts))
        window_seconds = max(1, int(window_seconds))
        connection = self._connection()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT password_hash, active FROM accounts WHERE study_id = ?",
                (study_id,),
            ).fetchone()
            if not account or not account["active"]:
                return "invalid"

            throttle = connection.execute(
                "SELECT failures, window_started_at FROM login_throttle WHERE study_id = ?",
                (study_id,),
            ).fetchone()
            if throttle and current_time - throttle["window_started_at"] < window_seconds:
                if throttle["failures"] >= max_attempts:
                    return "limited"
                failures = int(throttle["failures"])
                window_started_at = float(throttle["window_started_at"])
            else:
                failures = 0
                window_started_at = current_time

            if check_password_hash(account["password_hash"], password):
                connection.execute(
                    "DELETE FROM login_throttle WHERE study_id = ?", (study_id,)
                )
                return "authenticated"

            connection.execute(
                "INSERT INTO login_throttle(study_id, failures, window_started_at)"
                " VALUES(?, ?, ?)"
                " ON CONFLICT(study_id) DO UPDATE SET"
                " failures = excluded.failures,"
                " window_started_at = excluded.window_started_at",
                (study_id, failures + 1, window_started_at),
            )
            return "invalid"

    def _ensure_participant(
        self, connection: sqlite3.Connection, study_id: str, credit_limit: float
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO participants"
            "(study_id, schema_version, credit_limit_usd, pricing_policy_id)"
            " VALUES(?, ?, ?, ?)",
            (study_id, PARTICIPANT_DATA_VERSION, credit_limit, DEFAULT_PRICING_POLICY_ID),
        )

    def ensure_participant(self, study_id: str, credit_limit: float | None = None) -> None:
        study_id = self.validate_study_id(study_id)
        limit = float(credit_limit if credit_limit is not None else self.default_credit_limit)
        connection = self._connection()
        with connection:
            self._ensure_participant(connection, study_id, limit)

    def get_participant(self, study_id: str) -> dict:
        """Whole-document read, validated.

        Compatibility surface for tests, tooling and research export. The
        request path uses the targeted queries on `read()` / `write()` instead
        -- rebuilding the whole document is exactly the cost this store exists
        to avoid.
        """
        study_id = self.validate_study_id(study_id)
        self.ensure_participant(study_id)
        state = self.dump_participant(study_id)
        try:
            validate_participant_state(state)
        except ParticipantDataError as exc:
            raise StorageError(f"Invalid participant data: {study_id}") from exc
        return state

    def update_participant(self, study_id: str, mutator):
        """Whole-document read-modify-write. Not for the request path.

        Kept so tests and one-off scripts can seed or repair a participant with
        a dictionary mutator. Every row is rewritten, so
        the cost is the whole history.
        """
        study_id = self.validate_study_id(study_id)
        self.ensure_participant(study_id)
        with self.write(study_id) as transaction:
            state = transaction.dump()
            result = mutator(state)
            try:
                validate_participant_state(state)
            except ParticipantDataError as exc:
                raise StorageError("Participant update violates the current data format") from exc
            transaction.replace_all(state)
        return self.dump_participant(study_id), result

    def dump_participant(self, study_id: str) -> dict:
        """Rebuild a v2 participant document for tests and offline inspection."""
        return self.read(study_id).dump()

    def participant_ids(self) -> list[str]:
        return [
            row["study_id"]
            for row in self._connection().execute(
                "SELECT study_id FROM participants ORDER BY study_id"
            )
        ]


class ParticipantTx:
    """Targeted queries scoped to one participant.

    Instances handed out by `SqliteStore.write` sit inside a write
    transaction; those from `read` do not.
    """

    def __init__(self, connection: sqlite3.Connection, study_id: str):
        self._connection = connection
        self.study_id = study_id

    # ------------------------------------------------------------- meta

    def meta(self) -> dict:
        row = self._connection.execute(
            "SELECT schema_version, credit_limit_usd, pricing_policy_id"
            " FROM participants WHERE study_id = ?",
            (self.study_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"Unknown participant: {self.study_id}")
        return {
            "schemaVersion": row["schema_version"],
            "participantId": self.study_id,
            "creditLimitUsd": row["credit_limit_usd"],
            "pricingPolicyId": row["pricing_policy_id"],
        }

    def credit_limit(self) -> float:
        return float(self.meta()["creditLimitUsd"])

    def attachments(self) -> list[dict]:
        rows = self._connection.execute('SELECT * FROM attachments WHERE study_id = ? ORDER BY created_at, id', (self.study_id,))
        return [self._attachment_from_row(row) for row in rows]

    @staticmethod
    def _attachment_from_row(row):
        value = dict(row)
        value['manifest'] = json.loads(value['manifest'])
        value['provider_refs'] = json.loads(value['provider_refs'])
        return value

    def attachment(self, attachment_id):
        row = self._connection.execute('SELECT * FROM attachments WHERE study_id = ? AND id = ?', (self.study_id, attachment_id)).fetchone()
        return self._attachment_from_row(row) if row else None

    def insert_attachment(self, value):
        self._connection.execute(
            'INSERT INTO attachments(id, study_id, filename, size_bytes, stored_bytes, backend, status, created_at, updated_at, manifest) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (value['id'], self.study_id, value['filename'], value['size_bytes'], value['stored_bytes'], value['backend'],
             value['status'], value['created_at'], value['updated_at'], _dump(value['manifest'])),
        )

    def update_attachment(self, attachment_id, **changes):
        allowed = {'status', 'updated_at', 'manifest', 'provider_refs', 'message_id', 'stored_bytes'}
        if not changes or not set(changes) <= allowed:
            raise ValueError('Invalid attachment update')
        self._connection.execute(
            'UPDATE attachments SET ' + ','.join(key + '=?' for key in changes) + ' WHERE study_id=? AND id=?',
            [(_dump(value) if key in {'manifest', 'provider_refs'} else value) for key, value in changes.items()]
            + [self.study_id, attachment_id],
        )

    def delete_unbound_attachment(self, attachment_id):
        self._connection.execute('DELETE FROM attachments WHERE study_id=? AND id=? AND message_id IS NULL', (self.study_id, attachment_id))

    # ------------------------------------------------------------- reads

    def conversation(self, conversation_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM conversations WHERE study_id = ? AND id = ?",
            (self.study_id, conversation_id),
        ).fetchone()
        return None if row is None else self._conversation_from_row(row)

    def conversations(self) -> list[dict]:
        return [
            self._conversation_from_row(row)
            for row in self._connection.execute(
                "SELECT * FROM conversations WHERE study_id = ? ORDER BY seq",
                (self.study_id,),
            )
        ]

    def conversation_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM conversations WHERE study_id = ?",
            (self.study_id,),
        ).fetchone()
        return int(row["count"])

    def stored_message_char_count(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) AS chars"
            " FROM messages WHERE study_id = ?",
            (self.study_id,),
        ).fetchone()
        return int(row["chars"])

    def messages(self, conversation_id: str | None = None) -> list[dict]:
        if conversation_id is None:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE study_id = ? ORDER BY seq", (self.study_id,)
            )
        else:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            )
        return [self._message_from_row(row) for row in rows]

    def message(self, message_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM messages WHERE study_id = ? AND id = ?",
            (self.study_id, message_id),
        ).fetchone()
        return None if row is None else self._message_from_row(row)

    def messages_by_conversation(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for row in self._connection.execute(
            "SELECT * FROM messages WHERE study_id = ? ORDER BY seq", (self.study_id,)
        ):
            grouped.setdefault(row["conversation_id"], []).append(self._message_from_row(row))
        return grouped

    def message_stats_by_conversation(self) -> dict[str, tuple[int, str]]:
        """(개수, 마지막 createdAt)만 뽑는다. 본문은 건드리지 않는다.

        대화 목록은 이것만 있으면 되고, 이게 메시지를 전부 실어 보내지 않는
        쪽으로 옮겨갈 수 있는 이유다.
        """
        return {
            row["conversation_id"]: (row["count"], row["last_created_at"])
            for row in self._connection.execute(
                "SELECT conversation_id, COUNT(*) AS count,"
                "       MAX(created_at) AS last_created_at"
                "  FROM messages WHERE study_id = ? GROUP BY conversation_id",
                (self.study_id,),
            )
        }

    def has_messages(self, conversation_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (conversation_id,)
        ).fetchone()
        return row is not None

    def model_calls(self, conversation_id: str | None = None) -> list[dict]:
        if conversation_id is None:
            rows = self._connection.execute(
                "SELECT * FROM model_calls WHERE study_id = ? ORDER BY seq", (self.study_id,)
            )
        else:
            rows = self._connection.execute(
                "SELECT * FROM model_calls WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            )
        return [self._call_from_row(row) for row in rows]

    def call_timestamps_by_conversation(self) -> dict[str, list[str]]:
        """`updatedAt` inputs, without reading request/usage/provider payloads."""
        grouped: dict[str, list[str]] = {}
        for row in self._connection.execute(
            "SELECT conversation_id, started_at, completed_at FROM model_calls"
            " WHERE study_id = ? ORDER BY seq",
            (self.study_id,),
        ):
            grouped.setdefault(row["conversation_id"], []).append(
                row["completed_at"] or row["started_at"]
            )
        return grouped

    def conversation_call_timestamps(self, conversation_id: str) -> list[str]:
        return [
            row["completed_at"] or row["started_at"]
            for row in self._connection.execute(
                "SELECT started_at, completed_at FROM model_calls"
                " WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            )
        ]

    def usage_rows(self) -> list[dict]:
        """Just what cost accounting needs: no provider state, no content."""
        return [
            {"status": row["status"], "request": json.loads(row["request"]),
             "usage": _load(row["usage"])}
            for row in self._connection.execute(
                "SELECT status, request, usage FROM model_calls"
                " WHERE study_id = ? AND status = 'complete'",
                (self.study_id,),
            )
        ]

    def active_model_calls(self) -> list[dict]:
        """Started calls, with only the fields the concurrency guards read."""
        return [
            {"id": row["id"], "conversationId": row["conversation_id"],
             "startedAt": row["started_at"]}
            for row in self._connection.execute(
                "SELECT id, conversation_id, started_at FROM model_calls"
                " WHERE study_id = ? AND status = 'started' ORDER BY seq",
                (self.study_id,),
            )
        ]

    def global_active_model_call_count(self, started_after: str | None = None) -> int:
        """Count fresh started calls across every participant in this database."""
        if started_after is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM model_calls WHERE status = 'started'"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM model_calls"
                " WHERE status = 'started' AND started_at > ?",
                (started_after,),
            ).fetchone()
        return int(row["count"])

    def client_request_exists(self, client_request_id: str | None) -> bool:
        if client_request_id is None:
            return False
        row = self._connection.execute(
            "SELECT 1 FROM model_calls WHERE study_id = ? AND client_request_id = ? LIMIT 1",
            (self.study_id, client_request_id),
        ).fetchone()
        return row is not None

    def model_call(self, call_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM model_calls WHERE study_id = ? AND id = ?", (self.study_id, call_id)
        ).fetchone()
        return None if row is None else self._call_from_row(row)

    def provider_history(self, conversation_id: str, model_id: str) -> list[dict]:
        """Restorable provider state for one conversation and one exact model.

        Only completed calls in this conversation with matching request and
        provider-state model IDs are eligible. The payload stays separate
        from `model_calls` so ordinary record queries do not expose it.
        """
        rows = self._connection.execute(
            "SELECT c.output_message_id, p.schema_version, p.provider, p.model_id,"
            "       p.payload, p.extras"
            "  FROM model_calls c"
            "  JOIN model_call_provider_states p ON p.model_call_id = c.id"
            " WHERE c.conversation_id = ? AND c.status = 'complete'"
            "   AND c.request_model_id = ? AND p.model_id = ?"
            "   AND c.output_message_id IS NOT NULL"
            " ORDER BY c.seq",
            (conversation_id, model_id, model_id),
        )
        history = []
        for row in rows:
            entry = {
                "schemaVersion": row["schema_version"],
                "provider": row["provider"],
                "modelId": row["model_id"],
                "payload": json.loads(row["payload"]),
            }
            _merge_extras(entry, row["extras"])
            entry["outputMessageId"] = row["output_message_id"]
            history.append(entry)
        return history

    def web_searches(self, model_call_id: str | None = None) -> list[dict]:
        if model_call_id is None:
            rows = self._connection.execute(
                "SELECT payload FROM web_searches WHERE study_id = ? ORDER BY seq",
                (self.study_id,),
            )
        else:
            rows = self._connection.execute(
                "SELECT payload FROM web_searches WHERE model_call_id = ? ORDER BY seq",
                (model_call_id,),
            )
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------- writes

    def _next_seq(self, table: str) -> int:
        row = self._connection.execute(
            f"SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM {table} WHERE study_id = ?",
            (self.study_id,),
        ).fetchone()
        return int(row["next"])

    def initial_model_assignment(self) -> dict | None:
        row = self._connection.execute(
            "SELECT model_id, reasoning_effort, thinking_enabled, assigned_at"
            " FROM initial_model_assignments WHERE study_id = ?",
            (self.study_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "modelId": row["model_id"],
            "reasoningEffort": row["reasoning_effort"],
            "thinkingEnabled": bool(row["thinking_enabled"]),
            "assignedAt": row["assigned_at"],
        }

    def insert_initial_model_assignment(self, assignment: dict) -> None:
        self._connection.execute(
            "INSERT INTO initial_model_assignments"
            "(study_id, model_id, reasoning_effort, thinking_enabled, assigned_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                self.study_id,
                assignment["modelId"],
                assignment["reasoningEffort"],
                1 if assignment["thinkingEnabled"] else 0,
                assignment["assignedAt"],
            ),
        )

    def insert_conversation(self, conversation: dict, seq: int | None = None) -> None:
        self._connection.execute(
            "INSERT INTO conversations(id, study_id, seq, title, settings, created_at, extras)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                conversation["id"],
                self.study_id,
                self._next_seq("conversations") if seq is None else seq,
                conversation["title"],
                json.dumps(conversation["settings"], ensure_ascii=False),
                conversation["createdAt"],
                _extras(conversation, _CONVERSATION_KEYS),
            ),
        )

    def update_conversation(
        self, conversation_id: str, *, title: str | None = None, settings: dict | None = None
    ) -> None:
        assignments, values = [], []
        if title is not None:
            assignments.append("title = ?")
            values.append(title)
        if settings is not None:
            assignments.append("settings = ?")
            values.append(json.dumps(settings, ensure_ascii=False))
        if not assignments:
            return
        values.extend([self.study_id, conversation_id])
        self._connection.execute(
            f"UPDATE conversations SET {', '.join(assignments)}"
            " WHERE study_id = ? AND id = ?",
            values,
        )

    def insert_message(self, message: dict, seq: int | None = None) -> None:
        self._connection.execute(
            "INSERT INTO messages(id, study_id, conversation_id, seq, role, content,"
            " reasoning_summary, reasoning_duration_ms, response_parts, citations,"
            " created_at, extras) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message["id"],
                self.study_id,
                message["conversationId"],
                self._next_seq("messages") if seq is None else seq,
                message["role"],
                message["content"],
                _dump(message.get("reasoningSummary")),
                message.get("reasoningDurationMs"),
                _dump(message.get("responseParts")),
                _dump(message.get("citations")),
                message["createdAt"],
                _extras(message, _MESSAGE_KEYS),
            ),
        )

    def insert_model_call(self, call: dict, seq: int | None = None) -> None:
        self._connection.execute(
            "INSERT INTO model_calls(id, study_id, conversation_id, seq, client_request_id,"
            " client_instance_id, input_message_id, output_message_id, completed_at,"
            " request, request_model_id, status, started_at, error_code, usage,"
            " provider_result, extras)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call["id"],
                self.study_id,
                call["conversationId"],
                self._next_seq("model_calls") if seq is None else seq,
                call.get("clientRequestId"),
                call.get("clientInstanceId"),
                call.get("inputMessageId"),
                call.get("outputMessageId"),
                call.get("completedAt"),
                json.dumps(call["request"], ensure_ascii=False),
                call["request"]["modelId"],
                call["status"],
                call["startedAt"],
                call.get("errorCode"),
                _dump(call.get("usage")),
                _dump(call.get("providerResult")),
                _extras(call, _CALL_KEYS),
            ),
        )
        if call.get("providerState"):
            self.put_provider_state(call["id"], call["providerState"])

    def put_provider_state(self, call_id: str, provider_state: dict) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO model_call_provider_states"
            "(model_call_id, study_id, schema_version, provider, model_id, payload, extras)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                self.study_id,
                int(provider_state["schemaVersion"]),
                provider_state["provider"],
                provider_state["modelId"],
                json.dumps(provider_state["payload"], ensure_ascii=False),
                _extras(provider_state, _PROVIDER_STATE_KEYS),
            ),
        )

    def update_model_call(self, call_id: str, changes: dict) -> None:
        """Apply a partial update using JSON field names."""
        columns = {
            "outputMessageId": "output_message_id",
            "completedAt": "completed_at",
            "status": "status",
            "errorCode": "error_code",
        }
        json_columns = {"usage": "usage", "providerResult": "provider_result"}
        assignments, values = [], []
        extras_changes = {}
        for key, value in changes.items():
            if key in columns:
                assignments.append(f"{columns[key]} = ?")
                values.append(value)
            elif key in json_columns:
                assignments.append(f"{json_columns[key]} = ?")
                values.append(_dump(value))
            elif key == "providerState":
                if value:
                    self.put_provider_state(call_id, value)
            elif key in {"completionReason", "usageAccuracy"}:
                extras_changes[key] = value
            else:  # pragma: no cover - guards against a silent typo
                raise ParticipantDataError(f"Unknown model call field: {key}")
        if extras_changes:
            row = self._connection.execute(
                "SELECT extras FROM model_calls WHERE study_id = ? AND id = ?",
                (self.study_id, call_id),
            ).fetchone()
            extras = json.loads(row["extras"]) if row and row["extras"] else {}
            extras.update(extras_changes)
            assignments.append("extras = ?")
            values.append(json.dumps(extras, ensure_ascii=False))
        if not assignments:
            return
        values.extend([self.study_id, call_id])
        self._connection.execute(
            f"UPDATE model_calls SET {', '.join(assignments)} WHERE study_id = ? AND id = ?",
            values,
        )

    def insert_web_search(self, search: dict, seq: int | None = None) -> None:
        self._connection.execute(
            "INSERT INTO web_searches(id, study_id, model_call_id, seq, payload)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                search["id"],
                self.study_id,
                search["modelCallId"],
                self._next_seq("web_searches") if seq is None else seq,
                json.dumps(search, ensure_ascii=False),
            ),
        )

    def purge_provider_states(self, before: str | None = None) -> int:
        """Drop opaque provider state, leaving the research record intact.

        Purged conversations lose reasoning continuity for later turns but
        keep every message, usage figure and cost.
        """
        if before is None:
            cursor = self._connection.execute(
                "DELETE FROM model_call_provider_states WHERE study_id = ?", (self.study_id,)
            )
        else:
            cursor = self._connection.execute(
                "DELETE FROM model_call_provider_states WHERE model_call_id IN"
                " (SELECT id FROM model_calls WHERE study_id = ? AND started_at < ?)",
                (self.study_id, before),
            )
        return cursor.rowcount

    def replace_all(self, state: dict) -> None:
        """Rewrite every row for this participant from a whole v2 document."""
        bindings = [(a['id'], a['message_id']) for a in self.attachments() if a['message_id']]
        incoming = {m['id']: {a['id'] for a in m.get('attachments', [])} for m in state['messages']}
        if any(aid not in incoming.get(mid, set()) for aid, mid in bindings):
            raise StorageError('Cannot remove messages linked to retained research attachments')
        for aid, _ in bindings:
            self.update_attachment(aid, message_id=None)
        for table in (
            "web_searches",
            "model_call_provider_states",
            "model_calls",
            "messages",
            "conversations",
        ):
            self._connection.execute(f"DELETE FROM {table} WHERE study_id = ?", (self.study_id,))
        self._connection.execute(
            "UPDATE participants SET schema_version = ?, credit_limit_usd = ?,"
            " pricing_policy_id = ? WHERE study_id = ?",
            (
                int(state["schemaVersion"]),
                float(state["creditLimitUsd"]),
                state["pricingPolicyId"],
                self.study_id,
            ),
        )
        for index, conversation in enumerate(state["conversations"]):
            self.insert_conversation(conversation, seq=index)
        for index, message in enumerate(state["messages"]):
            self.insert_message(message, seq=index)
        for index, call in enumerate(state["modelCalls"]):
            self.insert_model_call(call, seq=index)
        for index, search in enumerate(state["webSearches"]):
            self.insert_web_search(search, seq=index)
        for aid, mid in bindings:
            self.update_attachment(aid, message_id=mid)

    # ------------------------------------------------------------- row mapping

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> dict:
        conversation = {
            "id": row["id"],
            "title": row["title"],
            "settings": json.loads(row["settings"]),
            "createdAt": row["created_at"],
        }
        return _merge_extras(conversation, row["extras"])

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> dict:
        message = {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
        }
        for key, column in _MESSAGE_OPTIONAL_JSON:
            if row[column] is not None:
                message[key] = json.loads(row[column])
        for key, column in _MESSAGE_OPTIONAL_SCALAR:
            if row[column] is not None:
                message[key] = row[column]
        message["createdAt"] = row["created_at"]
        return _merge_extras(message, row["extras"])

    def _call_from_row(self, row: sqlite3.Row, *, with_provider_state: bool = True) -> dict:
        call = {"id": row["id"], "conversationId": row["conversation_id"]}
        for key, column in _CALL_NULLABLE:
            call[key] = row[column]
        call["request"] = json.loads(row["request"])
        call["status"] = row["status"]
        call["startedAt"] = row["started_at"]
        for key, column in _CALL_OPTIONAL_JSON:
            if row[column] is not None:
                call[key] = json.loads(row[column])
        for key, column in _CALL_OPTIONAL_SCALAR:
            if row[column] is not None:
                call[key] = row[column]
        if with_provider_state:
            state = self._connection.execute(
                "SELECT schema_version, provider, model_id, payload, extras"
                " FROM model_call_provider_states WHERE model_call_id = ?",
                (row["id"],),
            ).fetchone()
            if state is not None:
                call["providerState"] = _merge_extras(
                    {
                        "schemaVersion": state["schema_version"],
                        "provider": state["provider"],
                        "modelId": state["model_id"],
                        "payload": json.loads(state["payload"]),
                    },
                    state["extras"],
                )
        return _merge_extras(call, row["extras"])

    # ------------------------------------------------------------- dump

    def dump(self) -> dict:
        state = self.meta()
        state["conversations"] = self.conversations()
        state["messages"] = self.messages()
        state["modelCalls"] = self.model_calls()
        state["webSearches"] = self.web_searches()
        return state
