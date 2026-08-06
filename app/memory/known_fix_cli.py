from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

import aiosqlite

from app.config.settings import Settings
from app.memory.factory import build_memory_conn_from_settings
from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes, register_known_fix
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import DEFAULT_EMBEDDING_PROVIDER_NAME, build_embedding_registry_from_settings


async def cmd_register(
    *,
    tenant_id: str,
    description: str,
    fixed_at: datetime,
    conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
) -> str:
    return await register_known_fix(
        conn,
        tenant_id=tenant_id,
        description=description,
        fixed_at=fixed_at,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
    )


async def cmd_list(*, tenant_id: str, conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    return await list_known_fixes(conn, tenant_id=tenant_id)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="已知故障修复登记管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register", help="登记一条已知故障修复")
    register_parser.add_argument("--tenant-id", required=True)
    register_parser.add_argument("--description", required=True, help="修复内容描述")
    register_parser.add_argument(
        "--fixed-at", required=True, help="修复时间，ISO格式，如 2026-08-05T00:00:00"
    )

    list_parser = subparsers.add_parser("list", help="列出已登记的修复记录")
    list_parser.add_argument("--tenant-id", required=True)

    return parser.parse_args()


async def _main() -> None:
    """CLI 入口。

    用法：
      python -m app.memory.known_fix_cli register --tenant-id t1 --description "网关超时问题已修复" --fixed-at 2026-08-05T00:00:00
      python -m app.memory.known_fix_cli list --tenant-id t1
    """
    args = _parse_args()
    settings = Settings()
    conn = await build_memory_conn_from_settings(settings)
    await ensure_known_fixes_schema(conn)

    if args.command == "register":
        embedding_registry = build_embedding_registry_from_settings(settings)
        fix_id = await cmd_register(
            tenant_id=args.tenant_id,
            description=args.description,
            fixed_at=datetime.fromisoformat(args.fixed_at),
            conn=conn,
            embedding_registry=embedding_registry,
            embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        )
        print(f"已登记修复记录 fix_id={fix_id}")
    elif args.command == "list":
        fixes = await cmd_list(tenant_id=args.tenant_id, conn=conn)
        if not fixes:
            print("没有已登记的修复记录。")
        for fix in fixes:
            fixed_at = datetime.fromtimestamp(fix["fixed_at"])
            print(f"[{fix['fix_id']}] {fix['description']} (修复时间: {fixed_at.isoformat()})")


if __name__ == "__main__":
    asyncio.run(_main())
