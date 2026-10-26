-- Co-Scientist participant store, schema version 2 data on SQLite.
--
-- The v2 collections keep their shape: rows that get filtered, counted, or
-- ordered become columns, and the leaves the application only ever handles
-- whole (settings, request, usage, responseParts, citations, a web search
-- trace) stay JSON text so nested provider and research records keep their
-- original structure.
--
-- Two rules drive the layout:
--
--   1. `providerState` lives in its own table. Provider-native payloads must
--      never reach a client and must be purgeable without touching messages
--      or usage records. A separate table makes
--      an accidental `SELECT *` on model_calls unable to leak it and makes
--      the purge one DELETE.
--
--   2. Every collection carries an explicit `seq`. The JSON store relied on
--      array insertion order, and that order is load-bearing: it is the order
--      messages are replayed to the provider. Timestamps are only incidentally
--      equivalent, so order is stored rather than inferred.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    study_id         TEXT PRIMARY KEY,
    password_hash    TEXT NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1,
    credit_limit_usd REAL NOT NULL
);

-- Persistent across Gunicorn workers and restarts. Rows exist only for real
-- accounts, so random unauthenticated IDs cannot grow this table.
CREATE TABLE IF NOT EXISTS login_throttle (
    study_id          TEXT PRIMARY KEY REFERENCES accounts(study_id) ON DELETE CASCADE,
    failures          INTEGER NOT NULL,
    window_started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    study_id          TEXT PRIMARY KEY,
    schema_version    INTEGER NOT NULL,
    credit_limit_usd  REAL NOT NULL,
    pricing_policy_id TEXT NOT NULL
);

-- The model setting a participant starts with, drawn once at random before
-- their first conversation. Kept apart from conversation settings so analysis
-- can tell the assigned starting point from choices the participant made later.
CREATE TABLE IF NOT EXISTS initial_model_assignments (
    study_id         TEXT PRIMARY KEY REFERENCES participants(study_id) ON DELETE CASCADE,
    model_id         TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    thinking_enabled INTEGER NOT NULL,
    assigned_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    study_id   TEXT NOT NULL REFERENCES participants(study_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    title      TEXT NOT NULL,
    settings   TEXT NOT NULL,          -- JSON object
    created_at TEXT NOT NULL,
    extras     TEXT                    -- JSON object of keys this schema does not map
);

CREATE INDEX IF NOT EXISTS conversations_by_participant
    ON conversations(study_id, seq);

CREATE TABLE IF NOT EXISTS messages (
    id                    TEXT PRIMARY KEY,
    study_id              TEXT NOT NULL,
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq                   INTEGER NOT NULL,
    role                  TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content               TEXT NOT NULL,
    -- NULL means the key was absent on the JSON row, which is how the v2 data
    -- distinguishes "not an assistant message" from "an empty list".
    reasoning_summary     TEXT,
    reasoning_duration_ms INTEGER,
    response_parts        TEXT,
    citations             TEXT,
    created_at            TEXT NOT NULL,
    extras                TEXT
);

CREATE INDEX IF NOT EXISTS messages_by_conversation
    ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS messages_by_participant
    ON messages(study_id, seq);

-- Originals and derived model inputs live in private object storage. Only
-- verified manifests are attached to messages. Unsent uploads can be purged;
-- sent research records must be retained with the conversation.
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES participants(study_id),
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    backend TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_id TEXT REFERENCES messages(id),
    manifest TEXT NOT NULL,
    provider_refs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS attachments_by_participant ON attachments(study_id, created_at);
CREATE INDEX IF NOT EXISTS attachments_by_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS model_calls (
    id                 TEXT PRIMARY KEY,
    study_id           TEXT NOT NULL,
    conversation_id    TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    -- Always present on the JSON row, but nullable.
    client_request_id  TEXT,
    client_instance_id TEXT,
    input_message_id   TEXT,
    output_message_id  TEXT,
    completed_at       TEXT,
    request            TEXT NOT NULL,  -- JSON object
    -- Lifted out of `request` so provider-state lookups can filter on the
    -- model without deserialising anything. Restoration requires an exact
    -- model ID match in both the request and the stored provider state.
    request_model_id   TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('started', 'complete', 'failed')),
    started_at         TEXT NOT NULL,
    -- Absent-when-NULL, like the optional message columns above.
    error_code         TEXT,
    usage              TEXT,
    provider_result    TEXT,
    extras             TEXT
);

CREATE INDEX IF NOT EXISTS model_calls_by_conversation
    ON model_calls(conversation_id, seq);
CREATE INDEX IF NOT EXISTS model_calls_by_participant
    ON model_calls(study_id, seq);
CREATE INDEX IF NOT EXISTS model_calls_by_participant_status
    ON model_calls(study_id, status);
CREATE INDEX IF NOT EXISTS model_calls_provider_history
    ON model_calls(conversation_id, request_model_id, status);

-- Invariants the JSON validator used to enforce by scanning the whole file.
-- As partial indexes they cost O(log n) and hold across processes, which the
-- old in-process lock never did.
CREATE UNIQUE INDEX IF NOT EXISTS model_calls_one_started_per_conversation
    ON model_calls(conversation_id) WHERE status = 'started';
CREATE UNIQUE INDEX IF NOT EXISTS model_calls_unique_client_request
    ON model_calls(study_id, client_request_id) WHERE client_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS model_call_provider_states (
    model_call_id  TEXT PRIMARY KEY REFERENCES model_calls(id) ON DELETE CASCADE,
    study_id       TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    provider       TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    payload        TEXT NOT NULL,  -- JSON object, opaque to us
    extras         TEXT
);

CREATE INDEX IF NOT EXISTS provider_states_by_participant
    ON model_call_provider_states(study_id);

CREATE TABLE IF NOT EXISTS web_searches (
    id            TEXT PRIMARY KEY,
    study_id      TEXT NOT NULL,
    model_call_id TEXT NOT NULL REFERENCES model_calls(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    -- Read only as a whole, by model call. Splitting executions, sources and
    -- citations into tables would add joins with nothing querying them.
    payload       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS web_searches_by_call
    ON web_searches(model_call_id);
CREATE INDEX IF NOT EXISTS web_searches_by_participant
    ON web_searches(study_id, seq);
