"""登录失败计数与锁定。

原凭证是一个 32 字节随机 token，爆破不现实；换成人选的密码之后熵一下子
掉到几十位，没有限流就是敞开的门。admin_auth_routes.py 的原注释自己就
写了"目前没有限流/锁定，这条日志是唯一的审计线索"。

状态存进程内存，和 session 同一形态。重启清空锁定是可接受的——攻击者
控制不了服务端的重启。

按 username 计数而不是按 IP：本系统部署在内网，IP 区分度低；而且按
username 锁定不会让一个攻击者顺带把所有人锁在门外。
"""
from __future__ import annotations

import time
from typing import Callable

__all__ = ["MAX_FAILURES", "LOCKOUT_SECONDS", "LoginLockedError", "LoginThrottle"]

MAX_FAILURES = 5
LOCKOUT_SECONDS = 900  # 15 分钟


class LoginLockedError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"登录失败次数过多，请 {retry_after_seconds // 60 + 1} 分钟后再试")
        self.retry_after_seconds = retry_after_seconds


class LoginThrottle:
    """username -> (连续失败次数, 首次失败时间)。

    已知局限：调用方只在**用户确实存在**时才 record_failure（见
    admin_auth_routes），所以攻击者可以用不断变化的伪造用户名无限尝试。
    这在只有个位数账号的内网系统里是可接受的——真正的账号仍受
    MAX_FAILURES 保护，而给任意用户名都建槽位会让内存被撑爆。
    """

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._failures: dict[str, tuple[int, float]] = {}

    def check(self, username: str) -> None:
        entry = self._failures.get(username)
        if entry is None:
            return
        count, first_failure_at = entry
        if count < MAX_FAILURES:
            return
        elapsed = self._now() - first_failure_at
        if elapsed >= LOCKOUT_SECONDS:
            del self._failures[username]
            return
        raise LoginLockedError(int(LOCKOUT_SECONDS - elapsed))

    def record_failure(self, username: str) -> None:
        count, first_failure_at = self._failures.get(username, (0, self._now()))
        self._failures[username] = (count + 1, first_failure_at)

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)
