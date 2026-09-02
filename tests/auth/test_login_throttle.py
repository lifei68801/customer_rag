from __future__ import annotations

import pytest

from app.auth.login_throttle import (
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    LoginLockedError,
    LoginThrottle,
)


def test_fresh_username_is_not_locked():
    LoginThrottle().check("alice")


def test_locks_after_max_failures():
    """原凭证是 32 字节随机 token，爆破不现实；换成人选的密码后熵急剧
    下降，没有限流就是敞开的门。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    with pytest.raises(LoginLockedError):
        throttle.check("alice")


def test_one_failure_short_of_the_limit_is_not_locked():
    """边界：第 5 次失败才锁。差一位就把人锁在门外，是自己给自己制造
    故障。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.check("alice")


def test_success_clears_the_counter():
    """密码打错几次然后打对了，不该在下次登录时留着旧账。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.record_success("alice")
    for _ in range(MAX_FAILURES - 1):
        throttle.record_failure("alice")
    throttle.check("alice")


def test_lock_expires_after_the_window():
    """锁定必须会过期。不过期的话，一次误操作就要重启服务才能救回来。"""
    now = [1000.0]
    throttle = LoginThrottle(now=lambda: now[0])
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    with pytest.raises(LoginLockedError):
        throttle.check("alice")

    now[0] += LOCKOUT_SECONDS + 1
    throttle.check("alice")


def test_still_locked_one_second_before_the_window_ends():
    """边界的另一侧：窗口没到就得一直锁着。"""
    now = [1000.0]
    throttle = LoginThrottle(now=lambda: now[0])
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    now[0] += LOCKOUT_SECONDS - 1
    with pytest.raises(LoginLockedError):
        throttle.check("alice")


def test_lockout_is_per_username():
    """按 username 计数而不是全局：否则一个攻击者能顺带把所有人锁在
    门外。"""
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    throttle.check("bob")


def test_error_carries_remaining_seconds():
    """只说"稍后再试"，用户会每隔 10 秒试一次。"""
    now = [1000.0]
    throttle = LoginThrottle(now=lambda: now[0])
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    now[0] += 60
    with pytest.raises(LoginLockedError) as exc_info:
        throttle.check("alice")
    assert exc_info.value.retry_after_seconds == LOCKOUT_SECONDS - 60


def test_window_starts_at_the_first_failure_not_the_last():
    """窗口从第一次失败起算。若每次失败都重置起点，攻击者只要保持敲击
    就能让锁永不过期——对他无所谓，对被锁的正常用户却是永久封禁。"""
    now = [1000.0]
    throttle = LoginThrottle(now=lambda: now[0])
    for _ in range(MAX_FAILURES):
        throttle.record_failure("alice")
    now[0] += LOCKOUT_SECONDS - 10
    throttle.record_failure("alice")  # 锁定期间又敲了一次
    now[0] += 11
    throttle.check("alice")
