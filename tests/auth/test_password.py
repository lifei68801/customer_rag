from __future__ import annotations

import base64

import pytest

from app.auth.password import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordTooLongError,
    PasswordTooShortError,
    hash_password,
    verify_password,
)


def test_same_password_hashes_differently_each_time():
    """salt 必须随机。两次哈希相同说明没加盐——那样一张彩虹表就够了，
    而且能一眼看出两个账号用了同一个密码。"""
    assert hash_password("correct horse") != hash_password("correct horse")


def test_correct_password_verifies():
    assert verify_password("correct horse", hash_password("correct horse")) is True


def test_wrong_password_does_not_verify():
    assert verify_password("wrong horse", hash_password("correct horse")) is False


def test_stored_format_is_self_describing():
    """参数必须写进存储串。写死在常量里的话，将来调参数会让所有历史密码
    一夜之间全部校验失败——而那时没人知道为什么。"""
    stored = hash_password("correct horse")
    parts = stored.split("$")
    assert parts[0] == "scrypt"
    assert parts[1:4] == ["16384", "8", "1"]
    assert len(base64.b64decode(parts[4])) == 16  # salt
    assert len(base64.b64decode(parts[5])) == 32  # dklen


def test_verifies_against_non_default_parameters():
    """参数从存储串读、不从常量读。这条是上一条的行为面：手工构造一个
    参数不同的存储串，仍然要能校验通过。"""
    import hashlib
    import os

    salt = os.urandom(16)
    digest = hashlib.scrypt(b"correct horse", salt=salt, n=1024, r=4, p=2, dklen=32)
    stored = (
        "scrypt$1024$4$2$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )
    assert verify_password("correct horse", stored) is True


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "notscrypt$16384$8$1$AAAA$BBBB",
        "scrypt$16384$8$1$AAAA",  # 段数不足
        "scrypt$notanumber$8$1$AAAA$BBBB",
        "scrypt$16384$8$1$!!!notbase64!!!$BBBB",
        # n 是天文数字：scrypt 会真的去分配那么多内存
        "scrypt$999999999999$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAA==",
    ],
)
def test_malformed_stored_value_does_not_verify(stored: str):
    """存储串损坏时返回 False，不抛异常。抛异常会让登录接口变成 500，
    而 500 和 401 对攻击者是两种不同的信号——它等于在说"这个用户存在，
    只是它的密码记录坏了"。"""
    assert verify_password("anything", stored) is False


def test_tampering_with_any_segment_breaks_verification():
    """逐段篡改都必须失败。只比对哈希段而忽略 salt 段的实现能通过前面
    几条，但会在这里露馅。"""
    stored = hash_password("correct horse")
    parts = stored.split("$")
    for index in (1, 2, 3, 4, 5):
        broken = list(parts)
        broken[index] = base64.b64encode(b"x" * 16).decode() if index >= 4 else "9999"
        assert verify_password("correct horse", "$".join(broken)) is False, f"第 {index} 段"


def test_too_short_password_is_rejected():
    with pytest.raises(PasswordTooShortError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


def test_minimum_length_password_is_accepted():
    """边界：正好 MIN_PASSWORD_LENGTH 位要能过。差一位就把人挡在门外，
    是自己给自己制造工单。"""
    assert verify_password("x" * MIN_PASSWORD_LENGTH, hash_password("x" * MIN_PASSWORD_LENGTH))


def test_too_long_password_is_rejected():
    """上限不是洁癖：scrypt 对超长输入没有保护，拿一个 10MB 的密码去登录
    就是一次免费的 CPU 消耗攻击。"""
    with pytest.raises(PasswordTooLongError):
        hash_password("x" * (MAX_PASSWORD_LENGTH + 1))
