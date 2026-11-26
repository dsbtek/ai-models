"""Tests for dev vs strict authentication modes, anti-spoofing, and strict (app_id, user_id) isolation."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.conftest import build_test_app


@pytest.mark.asyncio
async def test_dev_mode_allows_sandbox_but_protects_registered_apps():
    app, _ = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Keyless sandbox chat works out-of-the-box in dev mode
            r_sandbox = await client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "quick-sess"},
                json={"model": "llama-3.2-1b", "messages": [{"role": "user", "content": "Hello!"}]},
            )
            assert r_sandbox.status_code == 200
            assert r_sandbox.headers["x-app-id"] == "dev-sandbox"
            assert r_sandbox.headers["x-session-id"] == "quick-sess"

            # 2. Register a protected integrated app "crm-prod"
            r_reg = await client.post(
                "/v1/apps",
                json={"app_id": "crm-prod", "name": "CRM Production"},
            )
            assert r_reg.status_code == 201
            crm_key = r_reg.json()["api_key"]
            assert crm_key.startswith("ak_crm-prod_")

            # 3. Attempting to spoof X-App-Id: crm-prod WITHOUT its key in dev mode MUST fail (401)
            r_spoof = await client.post(
                "/v1/chat/completions",
                headers={"X-App-Id": "crm-prod", "X-User-Id": "alice"},
                json={"model": "llama-3.2-1b", "messages": [{"role": "user", "content": "Steal memories"}]},
            )
            assert r_spoof.status_code == 401

            # 4. Presenting crm-prod's key while sending mismatched X-App-Id: other-app MUST fail (403)
            r_mismatch = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {crm_key}", "X-App-Id": "other-app"},
                json={"model": "llama-3.2-1b", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert r_mismatch.status_code == 403

            # 5. Presenting crm-prod's valid key succeeds and resolves app_id = crm-prod
            r_valid = await client.get(
                "/v1/apps/me",
                headers={"Authorization": f"Bearer {crm_key}"},
            )
            assert r_valid.status_code == 200
            assert r_valid.json()["app_id"] == "crm-prod"
            assert r_valid.json()["authenticated"] is True


@pytest.mark.asyncio
async def test_strict_mode_and_key_rotation_revocation():
    app, _ = build_test_app(auth_mode="strict", admin_api_key="super-secret-admin")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Keyless call rejected in strict mode
            r_unauth = await client.get("/v1/models")
            assert r_unauth.status_code == 401

            # 2. Admin endpoint requires X-Admin-Key
            r_no_admin = await client.post("/v1/apps", json={"app_id": "erp", "name": "ERP"})
            assert r_no_admin.status_code == 401

            r_reg = await client.post(
                "/v1/apps",
                headers={"X-Admin-Key": "super-secret-admin"},
                json={"app_id": "erp", "name": "ERP"},
            )
            assert r_reg.status_code == 201
            old_key = r_reg.json()["api_key"]

            # 3. Valid key works
            assert (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {old_key}"})
            ).status_code == 200

            # 4. Rotate key -> old key immediately invalidated, new key works
            r_rot = await client.post(
                "/v1/apps/erp/rotate-key",
                headers={"X-Admin-Key": "super-secret-admin"},
            )
            assert r_rot.status_code == 200
            new_key = r_rot.json()["api_key"]
            assert new_key != old_key

            assert (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {old_key}"})
            ).status_code == 401
            assert (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {new_key}"})
            ).status_code == 200

            # 5. Deactivate app -> new key rejected too
            await client.delete(
                "/v1/apps/erp",
                headers={"X-Admin-Key": "super-secret-admin"},
            )
            assert (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {new_key}"})
            ).status_code == 401


@pytest.mark.asyncio
async def test_strict_cross_app_and_cross_user_memory_isolation():
    app, upstream_calls = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            key_a = (await client.post("/v1/apps", json={"app_id": "app-alpha", "name": "Alpha"})).json()["api_key"]
            key_b = (await client.post("/v1/apps", json={"app_id": "app-beta", "name": "Beta"})).json()["api_key"]

            # Store memory & session for user "shared-user" in app-alpha (even using identical session_id "s-100")
            r_mem_a = await client.post(
                "/v1/memories",
                headers={"Authorization": f"Bearer {key_a}", "X-User-Id": "shared-user"},
                json={"category": "preference", "importance": 5, "content": "Alpha secret stack is Elixir Phoenix"},
            )
            assert r_mem_a.status_code == 201

            await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key_a}",
                    "X-User-Id": "shared-user",
                    "X-Session-Id": "s-100",
                },
                json={"model": "llama-3.2-1b", "messages": [{"role": "user", "content": "Alpha confidential turn"}]},
            )

            # Query sessions, memories, semantic search, and chat as app-beta for the SAME user "shared-user" & session "s-100"
            r_mem_b = await client.get(
                "/v1/memories",
                headers={"Authorization": f"Bearer {key_b}", "X-User-Id": "shared-user"},
            )
            assert r_mem_b.status_code == 200
            assert r_mem_b.json()["data"] == []

            r_search_b = await client.post(
                "/v1/memories/search",
                headers={"Authorization": f"Bearer {key_b}", "X-User-Id": "shared-user"},
                json={"query": "Elixir Phoenix stack", "top_k": 5, "min_similarity": 0.0},
            )
            assert r_search_b.status_code == 200
            assert r_search_b.json()["data"] == []

            r_sess_b = await client.get(
                "/v1/sessions/s-100",
                headers={"Authorization": f"Bearer {key_b}", "X-User-Id": "shared-user"},
            )
            assert r_sess_b.status_code == 404

            # Also verify user-to-user isolation inside app-alpha ("other-user" cannot see "shared-user")
            r_other_user = await client.get(
                "/v1/memories",
                headers={"Authorization": f"Bearer {key_a}", "X-User-Id": "other-user"},
            )
            assert r_other_user.json()["data"] == []
