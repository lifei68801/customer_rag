from fastapi import Request, Response

from app.api.session_cookie import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    clear_session_cookies,
    is_secure_request,
    new_csrf_token,
    set_session_cookies,
)


def _scope(scheme: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    # 光给 "scheme" 不够：starlette 的 URL 只有在能算出 netloc（host 头或
    # "server"）时才会把 scheme 拼进最终 URL，否则 request.url.scheme 恒为
    # 空字符串，两个协议的断言都会失真。补上 "server" 让 netloc 能算出来。
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "headers": headers or [],
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "server": ("testserver", 80),
        }
    )


def test_session_cookie_is_http_only_and_csrf_cookie_is_not():
    """会话 token 必须 JS 读不到；CSRF token 必须 JS 读得到。

    双提交令牌的整个机制就建立在这个不对称上：攻击者的站点能让浏览器带上
    会话 Cookie，但读不到 CSRF 值、也就填不出那个请求头。两个都设成
    HttpOnly 的话前端拿不到 CSRF 值，机制直接失效；两个都不设的话 XSS
    能把会话 token 偷走。
    """
    response = Response()
    set_session_cookies(
        response, session_token="tok", csrf_token="csrf", secure=False, max_age=28800
    )
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    session_line = next(line for line in raw if line.startswith(SESSION_COOKIE_NAME))
    csrf_line = next(line for line in raw if line.startswith(CSRF_COOKIE_NAME))
    assert "HttpOnly" in session_line
    assert "HttpOnly" not in csrf_line


def test_session_cookie_declares_samesite_lax():
    response = Response()
    set_session_cookies(
        response, session_token="tok", csrf_token="csrf", secure=False, max_age=28800
    )
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    session_line = next(line for line in raw if line.startswith(SESSION_COOKIE_NAME))
    assert "samesite=lax" in session_line.lower()


def test_secure_flag_follows_the_request_scheme():
    """本地 http 开发时带上 Secure 会让 Cookie 直接不生效，所以这个标志
    按请求实际用的协议决定，不写死。"""
    assert is_secure_request(_scope("https")) is True
    assert is_secure_request(_scope("http")) is False


def test_clear_session_cookies_expires_both():
    response = Response()
    clear_session_cookies(response)
    raw = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    assert any(line.startswith(SESSION_COOKIE_NAME) for line in raw)
    assert any(line.startswith(CSRF_COOKIE_NAME) for line in raw)
    assert all('Max-Age=0' in line or 'max-age=0' in line.lower() for line in raw)


def test_new_csrf_token_is_unguessable_and_unique():
    a, b = new_csrf_token(), new_csrf_token()
    assert a != b
    assert len(a) >= 32
