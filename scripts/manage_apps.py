#!/usr/bin/env python3
"""CLI utility to register integrated apps and issue/rotate/revoke ak_... API keys."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROUTER_DIR = Path(__file__).resolve().parent.parent / "router"
sys.path.insert(0, str(ROUTER_DIR))

from config import load_config  # noqa: E402
from db.pool import DatabaseManager  # noqa: E402
from db.repositories import AppRepository  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.database_url:
        print("Error: DATABASE_URL is not set in .env or environment.", file=sys.stderr)
        return 1

    db = DatabaseManager(cfg.database_url)
    ok = await db.connect()
    if not ok:
        print(f"Error connecting to PostgreSQL: {db.last_error}", file=sys.stderr)
        return 1

    repo = AppRepository(db)
    try:
        if args.command == "create":
            settings = json.loads(args.settings) if args.settings else {}
            app_info, raw_key = await repo.create_app(
                app_id=args.app_id,
                name=args.name or args.app_id,
                settings=settings,
            )
            print(f"Registered app : {app_info['app_id']} ({app_info['name']})")
            print(f"Issued API key : {raw_key}")
            print("Save this key now — only its SHA-256 hash is stored in PostgreSQL.")
            return 0

        if args.command == "list":
            apps = await repo.list_apps()
            if not apps:
                print("No apps registered yet.")
                return 0
            print(f"{'APP_ID':<22} {'ACTIVE':<8} {'KEY_PREFIX':<20} {'SESSIONS':<10} {'MEMORIES':<10} NAME")
            print("-" * 88)
            for a in apps:
                print(
                    f"{a['app_id']:<22} {str(a['is_active']):<8} "
                    f"{(a['api_key_prefix'] or '(sandbox)'):<20} "
                    f"{a.get('session_count', 0):<10} {a.get('memory_count', 0):<10} {a['name']}"
                )
            return 0

        if args.command == "rotate-key":
            res = await repo.rotate_key(args.app_id)
            if res is None:
                print(f"Error: App '{args.app_id}' not found.", file=sys.stderr)
                return 1
            app_info, raw_key = res
            print(f"Rotated key for: {app_info['app_id']}")
            print(f"New API key    : {raw_key}")
            return 0

        if args.command == "revoke":
            deleted = await repo.delete_or_deactivate(args.app_id, hard=args.hard)
            if not deleted:
                print(f"Error: App '{args.app_id}' not found.", file=sys.stderr)
                return 1
            action = "Permanently deleted" if args.hard else "Deactivated"
            print(f"{action} app '{args.app_id}'.")
            return 0

        return 1
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage integrated applications and API keys for the llama.cpp router"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Register a new integrated app and issue an API key")
    p_create.add_argument("--app-id", required=True, help="Unique app slug (e.g. crm-web)")
    p_create.add_argument("--name", default="", help="Human-readable application name")
    p_create.add_argument("--settings", default="", help="Optional JSON object of per-app settings")

    sub.add_parser("list", help="List all registered and sandbox applications")

    p_rot = sub.add_parser("rotate-key", help="Rotate the API key for an existing app")
    p_rot.add_argument("--app-id", required=True, help="Target app_id")

    p_rev = sub.add_parser("revoke", help="Deactivate an app key (or --hard to delete all tenant data)")
    p_rev.add_argument("--app-id", required=True, help="Target app_id")
    p_rev.add_argument("--hard", action="store_true", help="Cascade delete sessions and memories")

    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
