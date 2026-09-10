"""数据库连接器。

三条硬约束，每一条都有专门的用例钉着：

1. **密码绝不进任何错误消息**（spec D3）。错误消息会进日志、会显示给用户、
   会被截图。
2. **只读**。这个功能没有任何理由写客户的生产库。
3. **driver 白名单**。driver 会拼进 SQLAlchemy 的 URL scheme，让调用方控制
   它等于开一个注入点。

被测数据库用 SQLite（`_TEST_DRIVERS` 那个显式开口），不需要起 MySQL 容器。
**它覆盖不到的是真实驱动能不能连上**——那一格靠人工验证。
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.ingestion.db_connector import (
    DbConnectionSpec,
    UnsafeQueryError,
    UnsupportedDriverError,
    check_connection,
    fetch_rows,
)


@pytest.fixture()
def goods_db(tmp_path: Path) -> Path:
    path = tmp_path / "goods.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE goods (id INTEGER, name TEXT)")
    conn.executemany(
        "INSERT INTO goods (id, name) VALUES (?, ?)",
        [(1, "可乐"), (2, "雪碧"), (3, "芬达")],
    )
    conn.commit()
    conn.close()
    return path


def _spec(db: Path | str = "", **over) -> DbConnectionSpec:
    payload = {
        "driver": "sqlite",
        "host": "",
        "port": 0,
        "database": str(db),
        "username": "",
    }
    payload.update(over)
    return DbConnectionSpec(**payload)


def test_an_unsupported_driver_is_refused_before_any_connection_attempt():
    """白名单之外的 driver 立刻拒绝。

    放行的话，driver 字符串就成了 SQLAlchemy URL scheme 的注入点。
    """
    with pytest.raises(UnsupportedDriverError):
        asyncio.run(check_connection(_spec(driver="'; DROP TABLE"), "pw"))


def test_a_non_select_query_is_refused(goods_db: Path):
    """只读。这个功能没有任何理由写客户的生产库。

    批次里同时有 SELECT 和非 SELECT——全是非 SELECT 的话，「一律拒绝」的
    实现也能变绿。
    """
    for bad in ["DELETE FROM goods", "UPDATE goods SET name='x'", "DROP TABLE goods"]:
        with pytest.raises(UnsafeQueryError):
            asyncio.run(fetch_rows(_spec(goods_db), "pw", bad))

    # 这一条必须通过，否则上面那组在「一律拒绝」的实现下也是绿的。
    columns, rows = asyncio.run(fetch_rows(_spec(goods_db), "pw", "SELECT * FROM goods"))
    assert len(rows) == 3


def test_a_select_with_a_trailing_write_is_refused(goods_db: Path):
    """「SELECT 1; DROP TABLE t」不能因为开头是 SELECT 就放行。

    只看首个单词的实现在这条上必红。
    """
    with pytest.raises(UnsafeQueryError):
        asyncio.run(fetch_rows(_spec(goods_db), "pw", "SELECT 1; DROP TABLE goods"))


def test_a_comment_cannot_smuggle_a_write_past_the_check(goods_db: Path):
    """注释开头不能把写操作藏过去。

    判据是「去掉注释和空白之后以 SELECT/WITH 开头」——不去注释的实现会
    因为首字符是 `-` 而拒绝掉合法查询，或者更糟：拿 `/* x */ DELETE ...`
    当成不是 SELECT 却又没拒绝。
    """
    with pytest.raises(UnsafeQueryError):
        asyncio.run(fetch_rows(_spec(goods_db), "pw", "-- 看起来人畜无害\nDELETE FROM goods"))

    # 反面：带注释的合法查询要能跑，否则上面那条在「见注释就拒」的实现下
    # 也是绿的。
    _, rows = asyncio.run(
        fetch_rows(_spec(goods_db), "pw", "-- 取全部商品\nSELECT * FROM goods")
    )
    assert len(rows) == 3


def test_fetch_rows_returns_column_names_alongside_the_rows(goods_db: Path):
    """列名是列映射那一步的输入。

    只返回行的话，用户得自己去数据库里查第几列是什么。
    """
    columns, rows = asyncio.run(
        fetch_rows(_spec(goods_db), "pw", "SELECT id, name FROM goods")
    )

    assert columns == ["id", "name"]
    assert rows[0] == (1, "可乐")


def test_limit_caps_the_preview(goods_db: Path):
    """预览只取前 N 行。

    不限制的话，「预览一下」会把两千万行拉进内存然后序列化成 JSON。
    """
    _, rows = asyncio.run(fetch_rows(_spec(goods_db), "pw", "SELECT * FROM goods", limit=2))

    assert len(rows) == 2


def test_no_limit_returns_everything(goods_db: Path):
    """不给 limit 时取全部。

    没有这一条，「恒取 2 行」的实现也能让上面那条变绿。
    """
    _, rows = asyncio.run(fetch_rows(_spec(goods_db), "pw", "SELECT * FROM goods"))

    assert len(rows) == 3


def test_a_connection_failure_message_does_not_contain_the_password():
    """密码绝不进错误消息。

    错误消息会进日志、会显示给用户、会被截图。**这条用例是本计划最重要的
    一条。**
    """
    spec = _spec(driver="postgresql", host="10.0.0.254", port=5432, database="d", username="u")

    try:
        asyncio.run(check_connection(spec, "hunter2"))
    except Exception as exc:
        assert "hunter2" not in str(exc)
        assert "hunter2" not in repr(exc)
        # 该说的还是要说清楚：连不上哪里。只剩一句"连接失败"的话，
        # 用户不知道是地址写错了还是网络不通。
        assert "10.0.0.254" in str(exc)
    else:
        pytest.fail("应该连不上")


def test_a_failure_while_fetching_rows_also_hides_the_password():
    """取数路径上的失败同样不能带出密码。

    只在 check_connection 里做脱敏的话，真正跑同步时那条异常照样把 DSN
    带出去——而那才是每天都会发生的路径。
    """
    spec = _spec(driver="mysql", host="10.0.0.254", port=3306, database="d", username="u")

    try:
        asyncio.run(fetch_rows(spec, "hunter2", "SELECT 1"))
    except Exception as exc:
        assert "hunter2" not in str(exc)
        assert "hunter2" not in repr(exc)
    else:
        pytest.fail("应该连不上")


def test_the_spec_object_cannot_carry_a_password():
    """`DbConnectionSpec` 是要被序列化进库的那个对象。

    它有 password 字段的话，早晚有人把整个对象 json.dumps 进 db_sources。
    """
    spec = DbConnectionSpec("mysql", "h", 3306, "d", "u")

    assert not hasattr(spec, "password")
    assert "password" not in spec.__dataclass_fields__


def test_the_spec_repr_has_nothing_secret_in_it():
    """dataclass 的 repr 会原样出现在日志和异常链里。

    没有 password 字段就不会有；这条用例是上一条的另一半——它钉的是
    "repr 出去的东西里没有密码"这个可观察结果。
    """
    spec = DbConnectionSpec("mysql", "10.0.0.5", 3306, "shop", "reader")

    assert "hunter2" not in repr(spec)
    assert "reader" in repr(spec)


def test_the_module_imports_without_the_database_driver_installed():
    """没装 sqlalchemy 时这个模块也要能 import。

    它被 `app/main.py` 间接 import。顶层 import 驱动的话，离线环境装不上
    那三个包时**整个后端起不来**，而不只是数据库导入这一页不可用——其余
    功能不该陪葬。跟仓库对 redis 的懒 import 约定一致。
    """
    import importlib
    import sys

    saved = {k: v for k, v in sys.modules.items() if k == "sqlalchemy" or k.startswith("sqlalchemy.")}
    saved_mod = sys.modules.pop("app.ingestion.db_connector", None)
    for key in saved:
        sys.modules.pop(key, None)
    sys.modules["sqlalchemy"] = None  # type: ignore[assignment]  # 让 import 立刻 ImportError
    try:
        module = importlib.import_module("app.ingestion.db_connector")
        assert module.DbConnectionSpec("mysql", "h", 3306, "d", "u").driver == "mysql"
        # 真要连的时候才需要驱动，那时给的是一句说清楚装什么的话，
        # 不是模块加载时炸、也不是一个 ImportError 堆栈冲成 500。
        with pytest.raises(module.UnsupportedDriverError, match="没有安装数据库驱动"):
            asyncio.run(module.check_connection(module.DbConnectionSpec("mysql", "h", 3306, "d", "u"), "pw"))
    finally:
        sys.modules.pop("sqlalchemy", None)
        sys.modules.update(saved)
        sys.modules.pop("app.ingestion.db_connector", None)
        if saved_mod is not None:
            sys.modules["app.ingestion.db_connector"] = saved_mod
