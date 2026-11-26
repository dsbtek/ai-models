# Containerized Multi-Model `llama.cpp` API, Memory Router & Playground

Run multiple CPU-only `llama.cpp` (`llama-server`) models in Docker behind a single OpenAI-compatible REST & streaming API (`http://localhost:8080/v1`) with:
- **Multi-Tenant Session & Semantic User Memory** backed by system PostgreSQL 16 + `pgvector` and local `all-minilm-l6-v2` embeddings.
- **Integrated App Registration & API Keys (`ak_<app_id>_...`)** with strict `(app_id, user_id)` isolation and a flexible `AUTH_MODE=dev` testing mode.
- **Built-in Interactive Web UI Playground** at [`http://localhost:8080/playground`](http://localhost:8080/playground).

---

## Quickstart

1. **Configure `.env`**
   ```bash
   cp .env.example .env
   ```
   Ensure `DATABASE_URL` points to your local PostgreSQL 16 database with the `vector` (`pgvector`) extension enabled:
   ```bash
   sudo -u postgres psql -d ai_models_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
   ```

2. **Download the default CPU-friendly GGUF models**
   ```bash
   chmod +x scripts/download-models.sh
   ./scripts/download-models.sh
   ```

3. **Start the containers**
   ```bash
   docker compose up -d --build
   ```

4. **Open the UI Playground or check health**
   - **Browser Playground**: `http://localhost:8080/playground`
   - **Health & Models**:
     ```bash
     curl -s http://localhost:8080/health
     curl -s http://localhost:8080/v1/models
     ```

---

## App Registration & Flexible Authentication (`AUTH_MODE`)

- **`AUTH_MODE=dev` *(default)***:
  - Quick `curl` and SDK tests work immediately without an API key under `"dev-sandbox"` (or any unclaimed `X-App-Id`).
  - Registered apps **still enforce** their issued `ak_...` API key in `dev` mode so test calls can never spoof a registered app.
- **`AUTH_MODE=strict` *(production)***:
  - Every `/v1/*` call must provide a valid `Authorization: Bearer ak_<app_id>_...` key.

### Register an Integrated App (CLI or REST)
```bash
# Via CLI
python3 scripts/manage_apps.py create --app-id crm-web --name "CRM Web App"
python3 scripts/manage_apps.py list

# Or via REST (from localhost in dev mode, or pass -H "X-Admin-Key: $ADMIN_API_KEY")
curl -s -X POST http://localhost:8080/v1/apps \
  -H "Content-Type: application/json" \
  -d '{"app_id": "crm-web", "name": "CRM Web App"}'
```

---

## Calling the API with Session & User Memory

### Stateful Multi-Turn Chat (`curl`)
Send only the latest user message per turn — the router automatically recalls long-term user facts, rolling summaries, and recent turns within the `2048`-token context budget:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ak_crm-web_..." \
  -H "X-User-Id: user_42" \
  -H "X-Session-Id: sess_onboarding" \
  -d '{
    "model": "general-assistant",
    "messages": [{"role": "user", "content": "I build backend services in Python with FastAPI and PostgreSQL."}],
    "max_tokens": 128
  }'
```

### Python (`openai` SDK)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="ak_crm-web_...",  # or "not-needed" in AUTH_MODE=dev
)

resp = client.chat.completions.create(
    model="coder",
    user="user_42",
    extra_body={"session_id": "sess_onboarding", "memory_mode": "auto"},
    messages=[{"role": "user", "content": "Create a healthcheck endpoint matching my stack."}],
)
print(resp.choices[0].message.content)
```

---

## Session & Memory Management Endpoints

All endpoints are strictly isolated to `(app_id, user_id)`:
- `GET /v1/apps/me` — Verify resolved `app_id` and usage stats
- `POST /v1/sessions` · `GET /v1/sessions` · `GET /v1/sessions/{id}` · `POST /v1/sessions/{id}/compact` · `DELETE /v1/sessions/{id}`
- `POST /v1/memories` · `GET /v1/memories` · `POST /v1/memories/search` · `PATCH /v1/memories/{id}` · `DELETE /v1/memories/{id}`
- `DELETE /v1/users/{user_id}/data` — Wipe all sessions and memories for a user within the app
