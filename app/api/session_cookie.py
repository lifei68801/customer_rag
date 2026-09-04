"""会话 Cookie 与 CSRF 令牌的名字、属性与读写。

集中在一个模块而不是散在登录/登出/依赖三处：Cookie 的名字和属性只要有
一处对不上（比如登出时 Path 写得和登录时不一样），浏览器就不会删掉它，
表现是"点了登出但还是登录着"——而三处各写一遍时这种不一致很难看出来。
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

SESSION_COOKIE_NAME = "customer_rag_session"
CSRF_COOKIE_NAME = "customer_rag_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

_COOKIE_PATH = "/"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def is_secure_request(request: Request) -> bool:
    """这个请求是不是经 HTTPS 到达的。

    Secure 标志不能写死：本地 http://localhost 开发时带上它，浏览器会
    直接忽略这个 Cookie，登录后表现为"登录成功但立刻又是未登录"。
    """
    return request.url.scheme == "https"


def set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    secure: bool,
    max_age: int,
) -> None:
    """下发会话 Cookie（HttpOnly）与 CSRF Cookie（非 HttpOnly）。

    两者的 HttpOnly 不对称是双提交令牌机制的基础：攻击者站点能让浏览器
    带上会话 Cookie，但读不到 CSRF 值、填不出那个请求头。
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path=_COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path=_COOKIE_PATH,
    )


def clear_session_cookies(response: Response) -> None:
    """删掉两个 Cookie。

    用 max_age=0 而不是 delete_cookie：属性（path/samesite）必须和下发时
    完全一致，浏览器才认这是同一个 Cookie，否则删不掉。
    """
    for name in (SESSION_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.set_cookie(
            name, "", max_age=0, samesite="lax", path=_COOKIE_PATH
        )
