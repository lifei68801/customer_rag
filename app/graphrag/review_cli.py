from __future__ import annotations

import argparse
import asyncio
from typing import Any

import aiosqlite

from app.config.settings import Settings
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.review_queue import (
    ReviewGraphClientProtocol,
    approve_review,
    list_pending_reviews,
    reject_review,
)


async def cmd_list(*, review_conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """列出所有待审核的候选关系，打印到终端并返回，便于测试断言。"""
    pending = await list_pending_reviews(review_conn)
    if not pending:
        print("没有待审核的候选关系。")
    for row in pending:
        print(
            f"[{row['review_id']}] {row['subject_candidate']} "
            f"--{row['relation_type']}--> {row['object_candidate']} "
            f"(原因: {row['reason']}, 时间: {row['created_at']})"
        )
    return pending


async def cmd_approve(
    *,
    review_conn: aiosqlite.Connection,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    graph_client: ReviewGraphClientProtocol,
) -> None:
    await approve_review(
        review_conn,
        review_id=review_id,
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        graph_client=graph_client,
    )
    print(
        f"已批准 review_id={review_id}，写入图谱："
        f"{subject_standard_name} -> {object_standard_name}"
    )


async def cmd_reject(
    *,
    review_conn: aiosqlite.Connection,
    review_id: int,
    note: str | None = None,
) -> None:
    await reject_review(review_conn, review_id=review_id, note=note)
    print(f"已驳回 review_id={review_id}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GraphRAG 人工待审核队列管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="列出所有待审核的候选关系")

    approve_parser = subparsers.add_parser("approve", help="批准一条候选关系并写入图谱")
    approve_parser.add_argument("review_id", type=int)
    approve_parser.add_argument(
        "--subject", required=True, help="人工确认的 subject 标准名（须已在术语表中）"
    )
    approve_parser.add_argument(
        "--object", required=True, help="人工确认的 object 标准名（须已在术语表中）"
    )

    reject_parser = subparsers.add_parser("reject", help="驳回一条候选关系（判定为噪声/误抽取）")
    reject_parser.add_argument("review_id", type=int)
    reject_parser.add_argument("--note", default=None, help="驳回原因备注")

    return parser.parse_args()


async def _main() -> None:
    """CLI 入口。

    用法：
      python -m app.graphrag.review_cli list
      python -m app.graphrag.review_cli approve 3 --subject 示例错误码E502 --object 示例登录模块
      python -m app.graphrag.review_cli reject 3 --note 确认是噪声
    """
    args = _parse_args()
    settings = Settings()
    review_conn = await build_review_conn_from_settings(settings)

    if args.command == "list":
        await cmd_list(review_conn=review_conn)
    elif args.command == "approve":
        graph_client = build_graph_client_from_settings(settings)
        await cmd_approve(
            review_conn=review_conn,
            review_id=args.review_id,
            subject_standard_name=args.subject,
            object_standard_name=args.object,
            graph_client=graph_client,
        )
    elif args.command == "reject":
        await cmd_reject(review_conn=review_conn, review_id=args.review_id, note=args.note)


if __name__ == "__main__":
    asyncio.run(_main())
