from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.graphrag.etl_staging import read_table_rows
from app.graphrag.schema_etl_row_processing import RowProcessingError


def test_read_table_rows_reads_csv_with_utf8_bom(tmp_path: Path):
    """Excel 的 CSV UTF-8 导出会在文件开头写 BOM。utf-8-sig 会剥掉它；
    如果退化成纯 utf-8，BOM 会粘在第一个列名前面，整份文件被误判成缺列。"""
    path = tmp_path / "with_bom.csv"
    path.write_bytes("名称,编号\n抹茶,A1\n".encode("utf-8-sig"))

    rows = list(read_table_rows(path))

    assert rows == [{"名称": "抹茶", "编号": "A1"}]


def test_read_table_rows_falls_back_to_gbk(tmp_path: Path):
    """国内 Excel 导出的 CSV 常见默认编码是 GBK，UTF-8 严格解码会失败。"""
    path = tmp_path / "gbk.csv"
    path.write_bytes("名称,编号\n抹茶,A1\n".encode("gbk"))

    rows = list(read_table_rows(path))

    assert rows == [{"名称": "抹茶", "编号": "A1"}]


def test_read_table_rows_reads_tsv_with_tab_delimiter(tmp_path: Path):
    path = tmp_path / "data.tsv"
    path.write_text("name\tcode\nfoo\tA1\n", encoding="utf-8")

    rows = list(read_table_rows(path))

    assert rows == [{"name": "foo", "code": "A1"}]


def test_read_table_rows_skips_xlsx_phantom_all_empty_rows(tmp_path: Path):
    """openpyxl 的 read_only 迭代会按已用范围补齐行数，被清空的行会
    产出全空的幽灵行。这些行必须安静跳过，不能当成缺列的脏数据。"""
    path = tmp_path / "phantom.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "code"])
    sheet.append(["foo", "A1"])
    sheet.append(["bar", "A2"])
    sheet["A3"] = None
    sheet["B3"] = None
    workbook.save(path)

    rows = list(read_table_rows(path))

    assert rows == [{"name": "foo", "code": "A1"}]


def test_read_table_rows_on_empty_xlsx_sheet_yields_nothing(tmp_path: Path):
    path = tmp_path / "empty.xlsx"
    workbook = Workbook()
    workbook.active.append(["name", "code"])
    workbook.save(path)

    assert list(read_table_rows(path)) == []


def test_read_table_rows_rejects_unsupported_suffix(tmp_path: Path):
    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(RowProcessingError, match="不支持的数据文件类型"):
        list(read_table_rows(path))


def test_read_table_rows_is_lazy(tmp_path: Path):
    """staging 必须保持流式：真实规模是 MUJI 一张 SKU 表 18 万+ 行。
    拿到迭代器时文件还没被遍历完，只有真正迭代才逐行产出。

    注意：这条断言只证明"迭代器能产出正确的第一行"，不能单独证明它是
    真流式——一个先把整份文件读完再包成 iter() 的急切实现，同样能让
    这条断言通过。真正能区分二者的判别在
    test_read_table_rows_does_not_touch_file_until_iterated 里，两条
    测试互补，缺一不可。"""
    path = tmp_path / "data.csv"
    path.write_text("name\nfoo\nbar\n", encoding="utf-8")

    iterator = read_table_rows(path)
    first = next(iterator)

    assert first == {"name": "foo"}


def test_read_table_rows_does_not_touch_file_until_iterated(tmp_path: Path):
    """真正的生成器函数在被调用时只是创建一个生成器对象，函数体一行都
    不会执行；只有第一次 next() 才会真正跑到第一条语句。用一个不存在的
    路径来验证这一点：如果 read_table_rows 是真生成器，调用它本身不应该
    抛 FileNotFoundError，只有 next() 才应该抛；如果它退化成"先把整个
    文件读完（或先打开文件）再包成迭代器返回"（比如 return iter(list(...))
    这种急切实现），文件不存在会在调用点本身就抛出来。

    这个退化正是 18 万+ 行场景下最需要防住的回归——一旦变成急切求值，
    "流式、不整份文件常驻内存"的约束就名存实亡了，而只断言"能拿到第一
    行"（见 test_read_table_rows_is_lazy）抓不住这种退化，因为急切实现
    同样能给出正确的第一行。"""
    missing_path = tmp_path / "does_not_exist.csv"

    iterator = read_table_rows(missing_path)  # 调用本身不应该抛异常

    with pytest.raises(FileNotFoundError):
        next(iterator)
