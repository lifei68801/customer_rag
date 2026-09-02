"""管理员密码的哈希与校验。

用标准库的 hashlib.scrypt（OpenSSL 实现，RFC 7914），不引入 bcrypt /
argon2-cffi——那两个都需要 cffi 或 Windows Build Tools，而本项目在
Windows 上开发。对一个内网管理后台，scrypt 参数选对了就够用，这不是
凑合。

存储格式自描述（scrypt$n$r$p$salt$hash），参数从存储串读取而不是从常量
读取：将来调高参数时，旧密码仍能校验通过，下次改密自动升级到新参数。
写死在常量里的话，调参数会让所有历史密码一夜之间全部失效，而那时没人
知道为什么。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "MAX_PASSWORD_LENGTH",
    "PasswordTooShortError",
    "PasswordTooLongError",
    "hash_password",
    "verify_password",
]

MIN_PASSWORD_LENGTH = 8
#: 上限不是洁癖：scrypt 对超长输入没有保护，一个 10MB 的密码就是一次
#: 免费的 CPU 消耗攻击。
MAX_PASSWORD_LENGTH = 1024

_DEFAULT_N = 16384
_DEFAULT_R = 8
_DEFAULT_P = 1
_SALT_BYTES = 16
_DK_LEN = 32

#: 校验时能接受的最大 n。损坏或恶意的存储串里 n 可能是个天文数字，
#: hashlib.scrypt 会真的按它去申请内存（内存开销约 128 * n * r 字节）。
#: 上限取默认值的 64 倍，留足将来调参的空间，同时挡住 OOM。
_MAX_ACCEPTED_N = _DEFAULT_N * 64


class PasswordTooShortError(ValueError):
    """密码短于 MIN_PASSWORD_LENGTH。"""


class PasswordTooLongError(ValueError):
    """密码长于 MAX_PASSWORD_LENGTH。"""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShortError(f"密码至少 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordTooLongError(f"密码最多 {MAX_PASSWORD_LENGTH} 个字符")
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_DEFAULT_N,
        r=_DEFAULT_R,
        p=_DEFAULT_P,
        dklen=_DK_LEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_DEFAULT_N),
            str(_DEFAULT_R),
            str(_DEFAULT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """存储串损坏时返回 False 而不是抛异常。

    抛异常会让登录接口变成 500，而 500 和 401 对攻击者是两种不同的信号
    ——它等于在说"这个用户存在，只是它的密码记录坏了"。
    """
    try:
        algorithm, n_raw, r_raw, p_raw, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        # 先挡住离谱的参数再进 scrypt：内存开销约 128 * n * r 字节，一个
        # 被篡改成天文数字的 n 会让这里直接 OOM，而不是干净地返回 False。
        if not (0 < n <= _MAX_ACCEPTED_N) or not (0 < r <= 64) or not (0 < p <= 16):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return secrets.compare_digest(actual, expected)
