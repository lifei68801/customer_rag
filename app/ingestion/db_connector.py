"""从关系型数据库只读地拉行。**唯一碰数据库驱动的地方。**

三条硬约束（spec D3）：

1. **密码绝不落库、绝不进日志、绝不进错误消息。** 连不上时说的是
   「连不上 10.0.0.5:3306」，不是把 DSN 原样吐出来。
2. **只读。** 生成的 SQL 只允许 SELECT/WITH。这个功能没有任何理由写客户的
   生产库。
3. **driver 白名单。** driver 会拼进 SQLAlchemy 的 URL scheme，让调用方控制
   它等于开一个注入点。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

#: 支持的数据库。写死白名单而不是接受任意字符串。
_DRIVER_URLS: dict[str, str] = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg",
}

#: 只在测试里开口的 driver。放在这里而不是散在测试代码里，是为了让
#: "生产上支持哪些库"这件事在一个地方读得完——测试用 SQLite 当被测数据库，
#: 不需要起一个 MySQL 容器。
_TEST_DRIVERS: dict[str, str] = {
    "sqlite": "sqlite",
}

#: 单条语句的注释。`--` 到行尾，以及 `/* */`。
#:
#: 去注释是判据的一部分而不是洁癖：不去的话 `-- 人畜无害\nDELETE FROM t`
#: 的首个非空字符是 `-`，"以 SELECT 开头"这条判断会拒绝掉它——看起来是对的，
#: 但同样会拒绝掉带注释的合法查询，于是下一个人会把判据放松成"包含 SELECT"，
#: 那才是真的漏。
_COMMENT_PATTERN = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

#: 只读判据：去掉注释和空白之后以 SELECT 或 WITH 开头。
_SELECT_PATTERN = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)


#: 部署里没装驱动时的提示。说清楚装什么，不是一个 ImportError 的堆栈。
_MISSING_DRIVER_MESSAGE = (
    "这个部署没有安装数据库驱动（sqlalchemy / pymysql / psycopg），数据库导入"
    "不可用。离线环境装不上的话，可以把数据导出成 CSV 走「表格导入」。"
)


class UnsupportedDriverError(Exception):
    """driver 不在白名单里，或者这个部署根本没装驱动。"""


class UnsafeQueryError(Exception):
    """这条 SQL 不是纯读。"""


class DbConnectionError(Exception):
    """连不上，或者查询执行失败。

    **消息里绝不含密码**：它会进日志、会显示给用户、会被截图。构造这个异常
    的地方（`_run`）负责保证这一点，调用方直接把 `str(exc)` 转给用户是安全的。
    """


@dataclass(frozen=True)
class DbConnectionSpec:
    """一个数据源的连接信息。

    **没有 password 字段**，这是刻意的：这个对象会被序列化进 `db_sources`
    表（spec D3 定的是「存连接不存密码」）。留一个 password 字段的话，早晚
    有人把整个对象 `json.dumps` 进库——而那时没有任何东西会报错。dataclass
    的 repr 也会把它带进日志和异常链。

    密码作为独立参数传给每个需要它的函数，永远不进这个对象。
    """

    driver: str
    host: str
    port: int
    database: str
    username: str


def _assert_safe_query(query: str) -> None:
    """只读判据。

    要点是**分号后面还有第二条语句就拒绝**：「SELECT 1; DROP TABLE t」的首个
    单词也是 SELECT，只看首个单词的实现会放它过去。结尾那个分号不算
    ——`SELECT 1;` 是一条语句。
    """
    stripped = _COMMENT_PATTERN.sub(" ", query).strip()
    if not _SELECT_PATTERN.match(stripped):
        raise UnsafeQueryError("只允许 SELECT / WITH 查询：这个功能不会写你的数据库")
    if stripped.rstrip(";").find(";") != -1:
        raise UnsafeQueryError("一次只能执行一条 SELECT：检测到分号后还有别的语句")


def _build_url(spec: DbConnectionSpec, password: str) -> Any:
    """拼连接 URL（返回 `sqlalchemy.engine.URL`）。

    sqlalchemy 在函数内 import 而不是模块顶层，跟 `app/memory/session_window_
    factory.py` 对 redis 的做法一致（见 pyproject.toml 里那段说明）：这个模块
    被 `app/main.py` 间接 import，顶层 import 的话，没装数据库驱动的部署
    **整个后端起不来**，而不只是"数据库导入这一页不可用"。离线环境确实
    可能装不上这三个包，那时其余功能不该陪葬。

    用 SQLAlchemy 的 `URL.create` 而不是自己拼字符串：它负责各字段的转义，
    密码里带 `@` 或 `/` 时手拼会拼出一个指向别处的地址。

    **返回值绝不进日志。** 它的 `__str__` 会把密码隐去（SQLAlchemy 的行为），
    但不要依赖那一点——本模块的做法是压根不打印它。
    """
    try:
        from sqlalchemy.engine import URL
    except ImportError:
        raise UnsupportedDriverError(_MISSING_DRIVER_MESSAGE) from None

    scheme = _DRIVER_URLS.get(spec.driver) or _TEST_DRIVERS.get(spec.driver)
    if scheme is None:
        raise UnsupportedDriverError(
            f"不支持的数据库类型 {spec.driver!r}，只支持：{'、'.join(sorted(_DRIVER_URLS))}"
        )
    if spec.driver == "sqlite":
        # SQLite 只在测试里用：没有主机、没有账号，database 就是文件路径。
        return URL.create("sqlite", database=spec.database)
    return URL.create(
        scheme,
        username=spec.username,
        password=password,
        host=spec.host,
        port=spec.port,
        database=spec.database,
    )


def _location(spec: DbConnectionSpec) -> str:
    """错误消息里用的位置描述。

    只有主机和端口——够用户判断"是不是地址写错了"，而不泄露账号密码。
    """
    return f"{spec.host}:{spec.port}" if spec.host else spec.database


def _run_sync(
    spec: DbConnectionSpec, password: str, query: str | None, limit: int | None
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """真正的阻塞调用，跑在线程里。

    整段包在一个 try 里，**异常一律换成 `DbConnectionError` 并只带位置**：
    驱动抛出来的原始异常经常把整个 DSN 带在消息里（密码就在其中），原样往上
    抛等于把密码写进日志。`from None` 而不是 `from exc`——链起来的话原始异常
    的消息仍然会出现在 traceback 里。
    """
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        raise UnsupportedDriverError(_MISSING_DRIVER_MESSAGE) from None

    url = _build_url(spec, password)
    try:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                if query is None:
                    return [], []
                result = connection.execute(text(query))
                columns = list(result.keys())
                rows = result.fetchmany(limit) if limit is not None else result.fetchall()
                return columns, [tuple(row) for row in rows]
        finally:
            engine.dispose()
    except Exception as exc:
        raise DbConnectionError(
            f"连不上 {_location(spec)}（{type(exc).__name__}）。"
            "请检查地址、端口、账号密码，以及这台机器能不能访问那个库。"
        ) from None


async def check_connection(spec: DbConnectionSpec, password: str) -> None:
    """连通性测试。连不通抛 `DbConnectionError`。

    名字不叫 `test_connection`：那样的话 pytest 会把它当成一条用例收集
    （默认 `python_functions = test*`），在导入它的用例文件里直接报
    "找不到 fixture"。HTTP 路径仍然是 `/db-import/test-connection`。
    """
    await asyncio.to_thread(_run_sync, spec, password, None, None)


async def fetch_rows(
    spec: DbConnectionSpec,
    password: str,
    query: str,
    *,
    limit: int | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """跑一条只读查询，返回 `(列名, 行)`。

    列名一起返回是因为列映射那一步照着它配——只给行的话，用户得自己回数据库
    里查第几列是什么。

    `limit` 给预览用。不限制的话，「预览一下」会把两千万行拉进内存然后序列化
    成 JSON。同步时不传 limit，取全部。
    """
    _assert_safe_query(query)
    return await asyncio.to_thread(_run_sync, spec, password, query, limit)
