"""测试套件的全局 fixture。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def deterministic_admin_seed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """给启动播种一个确定的初始管理员密码。

    `Settings` 会读开发机上的 `.env`，而那里的 `CUSTOMER_RAG_ADMIN_TOKEN`
    是什么、有多长，因人因机器而异。真实跑一遍 lifespan 的测试（`with
    TestClient(app)`）会连带跑启动播种，于是测试结果取决于开发者本机的配置
    ——短于 8 位就直接抛 AdminSeedError，而那和被测的东西毫无关系。

    覆盖成一个固定值，测试就只依赖代码。要验证"token 太短/为空会怎样"的
    用例，直接调 seed_admin_user 传参（见 tests/auth/test_bootstrap.py），
    不依赖环境变量。
    """
    monkeypatch.setenv("CUSTOMER_RAG_ADMIN_TOKEN", "test-seed-secret")
