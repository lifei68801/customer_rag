"""写路由的租户守卫：校验租户已启用，未启用就直接以 404 拒绝。

为什么是一个显式调用而不是 FastAPI 依赖：

这条守卫只加在**写**路由上。全部 40 个管理后台路由里，24 个写操作
（POST/PUT/DELETE）都有它，16 个读操作（GET）都没有，两个方向零例外——
这是一条被一致执行的策略（读一个停用租户的数据放行，写它不放行），不是
散落的疏漏。FastAPI 的 `dependencies=[...]` 只能挂在 router 或单个 endpoint
上，没有"只作用于写方法"这一档，挂到 router 级会连带给那 16 个读路由加上
守卫，改变它们的行为。

更硬的一条：这 24 个写路由的 tenant_id 有三个不同来源——路径参数、
`Form(...)` 多部分表单（admin_document_routes.py 的上传接口）、以及请求体
模型（duplicate_review/graph_review 的 payload.tenant_id）。一个声明了
`tenant_id: str` 的依赖会被 FastAPI 当成查询参数解析，对后两种直接 422。
依赖拿不到统一的租户来源，调用方传进来才行。

所以真正重复的不是"要不要调用"这件事（那是每个路由自己的策略决定），而是
调用之后那段把 TenantNotFoundError 翻译成 404 的四行样板——它此前在 6 个
路由文件里被逐字抄了 24 遍。这个模块只收敛翻译。
"""
from __future__ import annotations

import aiosqlite
from fastapi import HTTPException

from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant


async def require_active_tenant_or_404(
    review_conn: aiosqlite.Connection, tenant_id: str
) -> None:
    """租户不存在或已停用时抛 404，否则正常返回。

    "不存在"和"已停用"刻意合并成同一个响应：区分两者等于把"这个租户 ID
    是否存在"泄漏给未授权的调用方。
    """
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
