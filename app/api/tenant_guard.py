"""写路由的租户守卫：校验租户已启用，未启用就直接以 404 拒绝。

为什么是一个显式调用而不是 FastAPI 依赖：

这条守卫只加在**写**路由上。全部管理后台路由里，写操作（POST/PUT/DELETE）
都有它，读操作（GET）都没有，两个方向零例外——这是一条被一致执行的策略
（读一个停用租户的数据放行，写它不放行），不是散落的疏漏。FastAPI 的
`dependencies=[...]` 只能挂在 router 或单个 endpoint 上，没有"只作用于写
方法"这一档，挂到 router 级会连带给读路由加上守卫，改变它们的行为。

历史注记：此前还有第二条更硬的理由——tenant_id 有路径参数、`Form(...)`
多部分表单、请求体模型三种来源，一个声明了 `tenant_id: str` 的依赖会被
FastAPI 当成查询参数解析，对后两种直接 422，所以依赖根本拿不到统一的租户
来源。2026-09-02 的统一租户寻址改造已经把全部租户路由的 tenant_id 收敛为
路径参数，那条理由不再成立。留下"只作用于写方法"这一条。

注意它和租户**权限**校验是两件正交的事：这里管的是"这个租户还启用着吗"，
权限校验管的是"你有没有资格碰这个租户"。两者都要有。

这个模块收敛的是调用之后那段把 TenantNotFoundError 翻译成 404 的四行样板
——它此前在 6 个路由文件里被逐字抄了 24 遍。
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
