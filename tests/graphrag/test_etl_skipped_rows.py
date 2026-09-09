"""ETL 跳过的行落库。

跳过行此前只活在**单次 run 的报告**里（可下载 CSV），跨 run 查不了——
上周导入跳了 127 行这件事，今天没有任何地方能查到。而"哪些行进不来"正是
数据问题最直接的线索。
"""
import aiosqlite
import pytest

from app.graphrag.etl_skipped_rows import (
    SkippedRowRecord,
    count_skipped_rows,
    ensure_etl_skipped_rows_schema,
    list_skipped_rows,
    record_skipped_rows,
)


@pytest.fixture()
async def conn():
    connection = await aiosqlite.connect(":memory:")
    try:
        await ensure_etl_skipped_rows_schema(connection)
        yield connection
    finally:
        await connection.close()


def _row(row_number: int = 88, reason: str = "价格非数字", label: str = "产品"):
    return SkippedRowRecord(
        label=label, source_file="商品表.csv", row_number=row_number, reason=reason
    )


async def test_skipped_rows_survive_the_run_that_produced_them(conn):
    """跨 run 可查是这张表存在的全部理由。

    上周那次导入跳的行，今天要还能查到——report_json 里的那份随 run 详情
    页走，用户得先记得是哪一次跑批才找得到。
    """
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row()])
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-2", rows=[_row(row_number=12)])

    rows = await list_skipped_rows(conn, tenant_id="t1")

    assert {r["run_id"] for r in rows} == {"run-1", "run-2"}
    assert await count_skipped_rows(conn, tenant_id="t1") == 2


async def test_rows_are_scoped_to_the_tenant(conn):
    """两个租户各跑一次导入，各自只看到自己的。"""
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row()])
    await record_skipped_rows(conn, tenant_id="t2", run_id="run-2", rows=[_row(), _row(row_number=9)])

    assert [r["run_id"] for r in await list_skipped_rows(conn, tenant_id="t1")] == ["run-1"]
    assert await count_skipped_rows(conn, tenant_id="t1") == 1
    assert await count_skipped_rows(conn, tenant_id="t2") == 2


async def test_batch_write_keeps_row_numbers_and_reasons(conn):
    """行号和原因都要留着。

    「第 88 行 价格非数字」是用户据此去改表格的全部信息。只留原因不留行号
    的话，他得在两万行里自己找那一行。
    """
    await record_skipped_rows(
        conn, tenant_id="t1", run_id="run-1",
        rows=[_row(row_number=88, reason="价格非数字"), _row(row_number=104, reason="缺少必填列 名称")],
    )

    rows = await list_skipped_rows(conn, tenant_id="t1")

    assert {(r["row_number"], r["reason"]) for r in rows} == {
        (88, "价格非数字"),
        (104, "缺少必填列 名称"),
    }
    assert {r["source_file"] for r in rows} == {"商品表.csv"}
    assert {r["label"] for r in rows} == {"产品"}


async def test_recording_an_empty_batch_writes_nothing_and_does_not_error(conn):
    """一行没跳时不该在库里留下任何东西，也不该报错。

    绝大多数成功的导入都走这条路径。
    """
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[])

    assert await list_skipped_rows(conn, tenant_id="t1") == []
    assert await count_skipped_rows(conn, tenant_id="t1") == 0


async def test_the_same_run_recorded_twice_does_not_double_up(conn):
    """重跑同一个 run_id 时替换而不是追加。

    追加的话，重试一次导入就会让跳过行数翻倍——而用户以为问题变严重了。
    """
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row(), _row(row_number=9)])
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row()])

    assert await count_skipped_rows(conn, tenant_id="t1") == 1


async def test_an_empty_rerun_clears_the_previous_rows(conn):
    """重跑跳了 0 行时，上一次那些也要清掉。

    留着的话用户看到的是「上次那 127 行还在」——而这次导入已经全过了，
    他会跑回去改一份根本没问题的表格。
    """
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row()])
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[])

    assert await count_skipped_rows(conn, tenant_id="t1") == 0


async def test_the_newest_rows_come_first(conn):
    """最近一次导入的排最前面——入口是「刚才那次跳了什么」。"""
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-1", rows=[_row(row_number=1)])
    await record_skipped_rows(conn, tenant_id="t1", run_id="run-2", rows=[_row(row_number=2)])

    rows = await list_skipped_rows(conn, tenant_id="t1")

    assert [r["row_number"] for r in rows] == [2, 1]


async def test_paging_walks_the_whole_list_without_repeating(conn):
    """分页要能把整份列表走完，不重不漏。

    两万行的导入全跳掉是可能发生的（列名对不上），报错明细页必须分页。
    """
    await record_skipped_rows(
        conn, tenant_id="t1", run_id="run-1",
        rows=[_row(row_number=i) for i in range(5)],
    )

    first = await list_skipped_rows(conn, tenant_id="t1", limit=2, offset=0)
    second = await list_skipped_rows(conn, tenant_id="t1", limit=2, offset=2)
    third = await list_skipped_rows(conn, tenant_id="t1", limit=2, offset=4)

    seen = [r["row_number"] for r in first + second + third]
    assert sorted(seen) == [0, 1, 2, 3, 4]
