from __future__ import annotations

import re

__all__ = ["SAFE_TENANT_ID_PATTERN", "is_valid_tenant_id", "validate_tenant_id"]

# tenant_id 的全局字符集白名单。这个值会被拼进 Milvus 过滤表达式、当成
# 上传目录名、以及写进 SQLite 的租户列，因此校验必须是全系统一致的一份：
# 各层各写一份正则会出现"入口放行、底层拒绝"的错配（HTTP 接口返回 200 +
# job_id，文件已落盘，然后后台任务或 DELETE 在 Milvus 层炸掉）。
SAFE_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid_tenant_id(tenant_id: str) -> bool:
    """tenant_id 是否符合全局白名单字符集（ASCII 字母/数字/下划线/连字符）。"""
    return bool(tenant_id) and bool(SAFE_TENANT_ID_PATTERN.match(tenant_id))


def validate_tenant_id(tenant_id: str) -> None:
    """不合法就抛 ValueError。HTTP 层请改用 is_valid_tenant_id() 自行转 400。"""
    if not is_valid_tenant_id(tenant_id):
        raise ValueError(f"非法 tenant_id: {tenant_id!r}")
