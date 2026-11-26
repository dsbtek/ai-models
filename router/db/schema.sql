-- Multi-tenant session & semantic user memory schema for PostgreSQL 16 + pgvector

CREATE TABLE IF NOT EXISTS apps (
    app_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    api_key_hash    TEXT UNIQUE,
    api_key_prefix  TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    app_id              TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    title               TEXT,
    model_id            TEXT,
    rolling_summary     TEXT NOT NULL DEFAULT '',
    summary_up_to_seq   INTEGER NOT NULL DEFAULT 0,
    summary_token_est   INTEGER NOT NULL DEFAULT 0,
    message_count       INTEGER NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_tenant_active
    ON sessions (app_id, user_id, last_active_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
    ON sessions (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_messages (
    id          BIGSERIAL PRIMARY KEY,
    app_id      TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content     TEXT NOT NULL,
    token_est   INTEGER NOT NULL,
    model_id    TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (app_id, user_id, session_id)
        REFERENCES sessions (app_id, user_id, session_id) ON DELETE CASCADE,
    UNIQUE (app_id, user_id, session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq
    ON session_messages (app_id, user_id, session_id, seq DESC);

CREATE TABLE IF NOT EXISTS user_memories (
    memory_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id              TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL,
    category            TEXT NOT NULL DEFAULT 'fact'
                        CHECK (category IN ('preference', 'profile', 'fact', 'goal', 'constraint', 'episodic')),
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    embedding           vector(384) NOT NULL,
    importance          SMALLINT NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    confidence          REAL NOT NULL DEFAULT 0.85,
    source_session_id   TEXT,
    access_count        INTEGER NOT NULL DEFAULT 0,
    last_accessed_at    TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_by       UUID REFERENCES user_memories(memory_id) ON DELETE SET NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_memories_tenant_active
    ON user_memories (app_id, user_id, is_active);

CREATE INDEX IF NOT EXISTS idx_user_memories_expires_at
    ON user_memories (expires_at) WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_user_memories_embedding_hnsw
    ON user_memories USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS token_usage_daily (
    usage_date          DATE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::date,
    app_id              TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL DEFAULT '',
    model_id            TEXT NOT NULL,
    source              TEXT NOT NULL
                        CHECK (source IN (
                            'chat',
                            'completion',
                            'embedding',
                            'memory_extract',
                            'memory_compact',
                            'memory_embed'
                        )),
    request_count       INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       BIGINT NOT NULL DEFAULT 0,
    completion_tokens   BIGINT NOT NULL DEFAULT 0,
    total_tokens        BIGINT NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, app_id, user_id, model_id, source)
);

CREATE INDEX IF NOT EXISTS idx_token_usage_app_user_date
    ON token_usage_daily (app_id, user_id, usage_date DESC);
