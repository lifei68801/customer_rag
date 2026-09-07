"""批量删除的共享件：两级全选的模式判定、逐条执行、部分失败的结果形状。

管理后台一共有 8 个删除点（实体、关系边、文档、账号、租户、去重建议、
图谱审核、ETL 映射……），它们的守卫和写入各不相同，但"全选删除"这件事的
形状是同一个：

* 用户能勾**本页**（前端手里已经有这一页的 id），也能选**当前筛选条件下的
  全部**（条数可能是两万，前端手里没有、也不该有这份 id 列表）。两者是
  两种互斥的请求模式，破坏力差两个数量级，接口上不能靠猜；
* 一批里有的能删、有的被守卫挡住时，能删的删掉、挡住的逐条报出来，而不是
  一条失败整批回滚——两万条里只要有一条挡着，整次清理就永远做不成，用户
  还得先手工找出是哪一条；
* 结果必须让用户看得见：成功几条、哪几条没删掉、各自为什么。只弹一句
  "删除完成"就是静默失败。

这个模块只放跟具体资源无关的那部分。每个删除点自己提供"这一批要删哪些 key"
和"删掉一条"的实现。
"""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from fastapi import HTTPException
from pydantic import BaseModel

#: 两种模式同时给出/一个都不给时的说明。文案对准的是客户端开发者：这两种
#: 请求都不是用户能在界面上造出来的，是调用方的 bug。
_MODE_CONFLICT_DETAIL = (
    "node_keys（本页全选）和 filters（当前筛选条件下的全部）是两种互斥的模式，"
    "必须且只能给一个"
)


class BulkDeleteBlocked(Exception):
    """这一条删不掉，理由是 reason。

    reason 会原样出现在返回体里、被前端逐条展示给用户，所以它要写成用户能
    据此行动的样子（"被 3 条关系边使用，先去详情页删掉它们"），不是异常
    堆栈里那种给开发者看的措辞。
    """


class BulkDeleteFailure(BaseModel):
    """一条没删成的目标。

    key 是这个列表里定位一行的那个键——实体是 node_key，别的删除点是各自的
    id。故意不叫 node_key：这个模型要被 8 个删除点共用，叫 node_key 会让另外
    7 个地方的字段名说谎。
    """

    key: str
    reason: str


class BulkDeleteResult(BaseModel):
    """批量删除的结果。

    requested 和 deleted 都要给：只给 deleted 的话，用户看到"删除了 97 条"
    却不知道自己请求的是 100 条——差额正是他需要注意的那部分。failures 为空
    列表和"没有 failures 字段"不同，前者是"全都删掉了"这个明确结论。
    """

    requested: int
    deleted: int
    failures: list[BulkDeleteFailure]


def resolve_bulk_delete_mode(*, keys: Sequence[str] | None, filters: object | None) -> str:
    """判定这次请求走哪种模式，返回 "keys" 或 "filters"。

    两个都给或都不给时抛 400，不猜。猜的代价是不对称的：在"删这 3 条"和
    "删这个筛选条件下的两万条"之间选错，且删除不可撤销。

    注意判据是**字段在不在**，不是它的内容：filters 传了一个所有条件都为空的
    对象，语义是明确的"整租户全部"（没有任何筛选时的"全部"），不是"没给"。
    """
    if keys is not None and filters is not None:
        raise HTTPException(status_code=400, detail=_MODE_CONFLICT_DETAIL)
    if keys is None and filters is None:
        raise HTTPException(status_code=400, detail=_MODE_CONFLICT_DETAIL)
    return "keys" if keys is not None else "filters"


async def run_bulk_delete(
    keys: Sequence[str],
    delete_one: Callable[[str], Awaitable[None]],
) -> BulkDeleteResult:
    """逐条执行删除，收集失败明细。

    delete_one 抛 BulkDeleteBlocked 表示"这一条删不掉"，循环继续；抛别的
    异常则原样上抛——那是程序错误或基础设施故障，不该被伪装成"这条数据有
    问题"混进 failures 里让用户去猜。
    """
    deleted = 0
    failures: list[BulkDeleteFailure] = []
    for key in keys:
        try:
            await delete_one(key)
        except BulkDeleteBlocked as exc:
            failures.append(BulkDeleteFailure(key=key, reason=str(exc)))
        else:
            deleted += 1
    return BulkDeleteResult(requested=len(keys), deleted=deleted, failures=failures)
