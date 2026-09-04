"""管理后台路由形状的结构测试。

四组租户路由改成 /api/admin/{tenant_id}/xxx 之后，它们和非租户路由
（/api/admin/auth/*、/api/admin/tenants*）在同一个命名空间下。路由遮蔽
是静默的——被遮蔽的那条不会报错，只会永远匹配不到，或者匹配到错误的
处理函数。
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app


def _walk_routes(
    routes, prefix: str = "", inherited: frozenset = frozenset()
) -> list[tuple[str, APIRoute, frozenset]]:
    """展开成 (完整路径, APIRoute, 继承到的依赖函数集合) 列表。

    **必须递归展开**：FastAPI 0.141 起，`include_router()` 不再把子路由
    摊平进 `app.routes`，而是放一个 `_IncludedRouter` 包装对象，真正的
    APIRoute 藏在它的 `original_router.routes` 里，前缀藏在
    `include_context.prefix` 里。

    直接 `[r for r in app.routes if isinstance(r, APIRoute)]` 在这个版本上
    只能拿到 `/health` 一条——本文件的每条断言都会因为"没有路由可查"而
    静静地通过。那是这类结构测试最典型的死法：它测的东西一个都不存在，
    而它是绿的。

    第三项是沿途每一层的 `dependencies=[...]` 累积，两个来源都要取：
    `include_context.dependencies` 装的是**上一层** router 自己的依赖（实测
    如此），而这一层 router 自己在 `APIRouter(dependencies=[...])` 里声明的
    依赖只出现在 `original_router.dependencies` 上。只取前者的话，直接挂在
    app 上的 router（qa/agent/session 三个）的 router 级依赖一条都查不到——
    实测确认：只取 include_context 时 "/qa 是写接口却没有挂 CSRF 校验" 会
    误报，而那个依赖明明挂着。
    同一个版本变更还带来第二个坑：挂在父 router 上的依赖**不会**合并进
    各条 `APIRoute.dependant`（已实测确认恒为 False），但运行时照常执行
    （也已实测：父 router 依赖抛 403 时请求真的返回 403）。按 dependant
    查的话，"每条租户路由都挂了校验"会全部报红，而反向那条"非租户路由
    没挂校验"会全部通过——一条永远为真的断言。
    """
    found: list[tuple[str, APIRoute, frozenset]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route, inherited))
        elif type(route).__name__ == "_IncludedRouter":
            ctx = route.include_context
            sub_prefix = getattr(ctx, "prefix", "") or ""
            deps = {d.dependency for d in (getattr(ctx, "dependencies", None) or [])}
            deps |= {d.dependency for d in (route.original_router.dependencies or [])}
            found.extend(
                _walk_routes(route.original_router.routes, prefix + sub_prefix, inherited | deps)
            )
    return found


def _admin_paths() -> list[str]:
    return [p for p, _, _ in _walk_routes(app.routes) if p.startswith("/api/admin")]


def test_the_walker_actually_finds_routes():
    """守住上面那个假绿：遍历器必须真的找得到管理后台的路由。

    没有这一条，_walk_routes 哪天因为 FastAPI 内部结构再变而返回空列表，
    这个文件里其余的断言会全部"通过"。
    """
    paths = _admin_paths()
    assert len(paths) > 30, f"只找到 {len(paths)} 条管理后台路由，遍历器多半失效了"


def test_four_route_groups_are_tenant_scoped():
    """这四组必须带上租户段。少一组就是少一块将来校验不到的地方。"""
    paths = _admin_paths()
    for suffix in ("nav-badges", "duplicate-reviews", "graph-reviews", "documents"):
        matching = [p for p in paths if suffix in p]
        assert matching, f"没有找到 {suffix} 的任何路由"
        for path in matching:
            assert "{tenant_id}" in path, f"{path} 缺少租户段"


#: 租户**作用域**路由的前缀白名单——请求操作的数据属于这个租户。
#: 这些将来要挂租户访问校验（member 只能碰自己的那个）。
_TENANT_SCOPED_PREFIXES = (
    "/api/admin/{tenant_id}/",
    "/api/admin/ontology/{tenant_id}/",
)

#: 不属于任何租户的路由。
#:
#: /api/admin/tenants/{tenant_id}/disable 也在这里——它路径里虽然有
#: {tenant_id}，但那是**被操作的对象**（要禁用哪个租户），不是**操作发生
#: 的作用域**。这个区别是要命的：给它挂上"租户访问校验"的话，member 对
#: 自己所属的租户会顺利通过校验，于是就能把自己所在的租户停掉。它必须靠
#: 管理员专属权限保护，而不是靠租户作用域校验。
_NON_TENANT_PREFIXES = (
    "/api/admin/auth/",
    "/api/admin/tenants",
    "/api/admin/accounts",
)


def test_every_admin_route_is_classified():
    """每条管理后台路由要么是租户作用域的，要么明确不是。

    没有"忘了归类"这一档：未归类的路由在将来上租户校验时会被漏掉，而漏掉
    的那条是越权读写，不会有任何报错。
    """
    unclassified = [
        p
        for p in _admin_paths()
        if not p.startswith(_TENANT_SCOPED_PREFIXES) and not p.startswith(_NON_TENANT_PREFIXES)
    ]
    assert not unclassified, (
        f"这些路由既不在租户作用域白名单里，也不在非租户白名单里：{unclassified}。"
        "新增路由时必须在本文件里显式归类。"
    )


def test_tenant_scoped_routes_all_carry_the_tenant_segment():
    """归为租户作用域的，路径里必须真的有 {tenant_id}——否则校验无从下手。"""
    for path in _admin_paths():
        if path.startswith(_TENANT_SCOPED_PREFIXES):
            assert "{tenant_id}" in path, f"{path} 归为租户作用域却没有租户段"


def test_auth_routes_never_carry_a_tenant_segment():
    """登录接口加上租户段会让 FastAPI 把 tenant_id 当成必填参数，直接
    422——而那时谁也进不来。"""
    for path in _admin_paths():
        if path.startswith("/api/admin/auth/"):
            assert "{tenant_id}" not in path, f"{path} 不该有租户段"


def test_no_static_admin_route_is_shadowed_by_the_tenant_wildcard():
    """/api/admin/{tenant_id}/xxx 是通配路径，它不能吃掉同形状的静态路径。

    例：/api/admin/auth/login 与 /api/admin/{tenant_id}/documents 段数相同，
    两者第 4 段不同所以安全。但将来若新增 /api/admin/auth/documents，它就会
    被通配路径遮蔽——请求落到错误的处理函数上，且不会有任何报错。
    """
    wildcard_suffixes = {
        p.split("/")[4]
        for p in _admin_paths()
        if len(p.split("/")) > 4 and p.split("/")[3] == "{tenant_id}"
    }
    shadowed = [
        p
        for p in _admin_paths()
        if len(p.split("/")) > 4
        and not p.split("/")[3].startswith("{")
        and p.split("/")[4] in wildcard_suffixes
    ]
    assert not shadowed, f"这些路由会被租户通配路径遮蔽：{shadowed}"


def test_every_tenant_scoped_route_checks_tenant_access():
    """挂载层强制的兜底。

    人的记性在第 9 个 router 上一定会失效，而漏掉的那条不会有任何运行时
    报错——请求照常 200，只是返回的是别人租户的数据。
    """
    from app.api.deps import require_tenant_access

    checked = 0
    for path, _route, inherited in _walk_routes(app.routes):
        if not path.startswith(_TENANT_SCOPED_PREFIXES):
            continue
        assert require_tenant_access in inherited, f"{path} 没有挂租户权限校验"
        checked += 1
    # 一条都没查到的话，上面的循环体从没执行过，这个断言就是空的。
    assert checked > 20, f"只检查了 {checked} 条租户路由，遍历器多半失效了"


#: 必须挂 CSRF 校验的路由前缀：整个管理后台，加上前台那五个走会话认证的
#: 接口。/voice/* 不在其中——它今天仍然是匿名入口，没有会话 Cookie 可言，
#: 归属另一件事（见最终评审 finding 5）。
_CSRF_REQUIRED_PREFIXES = ("/api/admin", "/agent", "/qa")


def test_every_write_route_behind_a_session_checks_csrf():
    """挂载层强制的兜底，跟上面那条租户校验同一个理由。

    会话改用 Cookie 之后，浏览器会自动给同源请求附带凭证，于是每一个写
    接口都进入了 CSRF 的射程。漏挂一条不会有任何运行时报错，它只是一条
    活着的 CSRF 通道——改密码、建号、停租户、传文档都在这一类里。
    """
    from app.api.deps import require_csrf

    checked = 0
    for path, route, inherited in _walk_routes(app.routes):
        if not path.startswith(_CSRF_REQUIRED_PREFIXES):
            continue
        if not (route.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        assert require_csrf in inherited, f"{path} 是写接口却没有挂 CSRF 校验"
        checked += 1
    # 一条都没查到的话，上面的循环体从没执行过，这个断言就是空的。
    assert checked > 20, f"只检查了 {checked} 条写路由，遍历器多半失效了"


def test_non_tenant_routes_do_not_check_tenant_access():
    """反向断言。

    给登录接口挂上租户校验会让 FastAPI 把 tenant_id 当成必填查询参数，
    登录直接 422——那时谁也进不来。
    """
    from app.api.deps import require_tenant_access

    for path, _route, inherited in _walk_routes(app.routes):
        if not path.startswith("/api/admin") or path.startswith(_TENANT_SCOPED_PREFIXES):
            continue
        assert require_tenant_access not in inherited, f"{path} 不该挂租户权限校验"
