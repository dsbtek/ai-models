-- Daily-rollup token usage per (app_id, user_id, model_id, source).

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
