"""ETL 三层管道的第一层：staging——解析与类型归一。

xlsx/csv/tsv/xls → 统一的 dict[str, str] 行序列。这一层不知道本体、
不知道 node_key，只负责"把文件变成行"。2026-08-30 从 schema_etl.py
原样提取，行为一行未改。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator, Sequence

import xlrd
from openpyxl import load_workbook

from app.graphrag.schema_etl_row_processing import RowProcessingError, convert_excel_cell_to_string
from app.graphrag.source_parse_options import SourceParseOptions


def deduplicate_header(names: Sequence[str]) -> list[str]:
    """给重名列加 " (2)"、" (3)" 后缀，让每一列都有自己的键。

    不这么做的话，把表头直接当 dict 键会让后出现的同名列**静默覆盖**先
    出现的：整列数据消失，而跑批报告仍然是"成功"。重名列在真实的表里很
    常见——MUJI 的 SKU 主数据表有 Color×2（英语/現地語）、Size×2、
    Retail price×4、Depth/Width/Height/Weight 各 ×3（ボール/ケース/
    ピース），一张表 113 列曾经被读成 6 列。

    选后缀而不是报错：重名列本身是合法的表达（同一个概念的多语言列、
    多规格列），用户需要在字段映射那一步看到全部列、自己挑用哪一个；
    直接报错会让这类表根本导不进来。

    空列名彼此也算重名，同样参与去重——否则几十列没有表头的列会全部挤
    进同一个 "" 键里互相覆盖。
    """
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        candidate = name
        suffix = 1
        # 表头里可能本来就有一列叫 "Color (2)"，所以不能算出后缀就直接用，
        # 得一直往后找到一个没被占用的名字，否则又退回到互相覆盖。
        while candidate in seen:
            suffix += 1
            candidate = f"{name} ({suffix})"
        seen.add(candidate)
        result.append(candidate)
    return result



def trim_trailing_empty_names(names: Sequence[str]) -> list[str]:
    """砍掉行尾那些名字为空的列。

    四个读取器对"这张表有几列"的口径本来各不相同：xlrd（.xls）按实际存在的
    单元格记录算，openpyxl 的 read_only 模式按工作表声明的 dimension 算，
    前端 SheetJS 按 !ref 算。手工编辑过的 Excel 里声明范围常常比实际数据宽，
    于是同一张表在两端得到不同的列数——真实的 MUJI .xls 是前端 114 / 后端
    113，而 dimension 被撑宽的 .xlsx 反过来是前端 3 / 后端 6。列数对不上，
    跑批前的逐列对账会直接判 400，这张表根本导不进来。

    这条规则前后端逐字对应（前端 sourceParser.ts::trimTrailingEmptyNames）：
    取到表头行之后、去重之前，砍掉**行尾连续的**空名列。

    只砍尾部：中间的空名列一列都不能动。MUJI 那张表第 52~56 列就是中间的
    空名列，砍掉会让后面所有列的位置整体左移——而列数还是对得上的，对账
    发现不了。

    必须在 deduplicate_header **之前**砍：反过来的话 " (2)" 这类后缀是按砍
    之前的位置算出来的，砍掉之后编号会错乱。

    代价：行尾那些没有列名、下面却有数据的列会被丢掉。这类列今天本来也无法
    在字段映射里被引用（名字是空的），丢掉它换列数口径一致。
    """
    end = len(names)
    while end > 0 and names[end - 1].strip() == "":
        end -= 1
    return list(names[:end])


def header_from(raw_names: Sequence[str]) -> list[str]:
    """原始表头 → 可用作列键的表头。两步的顺序是这一处说了算，三个读取器
    都走它，免得某一条路径漏掉一步或把顺序做反。"""
    return deduplicate_header(trim_trailing_empty_names(raw_names))

def _detect_text_encoding(path: Path) -> str:
    """CSV/TSV 源文件的编码探测：优先按 UTF-8 严格解码，失败则回退尝试
    GBK（国内 Excel 导出 CSV 最常见的默认编码）——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 6。这里读一遍原始字节只是为了做 decode 测试，不保留解码结果；
    真正的行级处理仍然通过 csv.reader 用确定的编码重新打开文件、
    流式进行，不会把整份解码后的文本一次性留在内存里。两种编码都解码
    失败时，让 GBK 阶段的 UnicodeDecodeError 原样往上抛，不做进一步猜测。

    UTF-8 分支返回 "utf-8-sig" 而不是 "utf-8"：Excel 的"CSV UTF-8"导出
    格式会在文件开头写一个 BOM，纯 "utf-8" 编码不会因为这个 BOM 报解码
    错误，但会把它解码成字面的 U+FEFF 字符粘在第一个字段名前面，导致
    表头第一列对不上用户看到的列名、整份文件被误判成"缺列"全部跳过。
    "utf-8-sig" 对没有 BOM 的普通 UTF-8 文件解码结果完全一样，只在文件
    真的带 BOM 时才会正确剥掉它，是纯粹的超集写法。
    """
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        return "utf-8-sig"
    except UnicodeDecodeError:
        pass
    raw.decode("gbk")
    return "gbk"


def _read_delimited_rows(
    path: Path, *, delimiter: str, options: SourceParseOptions
) -> Iterator[dict[str, str]]:
    """逐行流式产出 CSV/TSV 源文件的行——设计文档第 6.4 节给出的真实规模
    是"MUJI 一张 SKU 表 18 万+ 行"。注意：编码探测阶段（见
    _detect_text_encoding）会把整个文件读一遍原始字节做 decode 测试，
    这一步不是流式的；探测完成后的逐行处理本身才是流式、不整份文本
    常驻内存。跳到 header_row 同样是流式的——只是把前面的记录读过去丢掉，
    不会把它们攒起来。"""
    encoding = _detect_text_encoding(path)
    with path.open(encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        # header_row 数的是 CSV **记录**，不是物理行：带引号的字段里可以有
        # 换行，那种记录横跨多个物理行。用户在 Excel 里看到的行号也是记录号，
        # 按物理行数会跟他看到的对不上。
        header: list[str] | None = None
        for record_number, row in enumerate(reader, start=1):
            if record_number == options.header_row:
                header = header_from(row)
                break
        if header is None:
            # 表头行号超出文件长度。产出空序列，跟"空文件"同一个终态。
            return
        first_data_row = options.resolved_first_data_row
        for record_number, row in enumerate(reader, start=options.header_row + 1):
            if record_number < first_data_row:
                continue
            # csv.DictReader 会跳过空行，这里手工复现同样的行为。
            if not row:
                continue
            # 值比表头少时补 None，跟 DictReader 的 restval 默认值一致；
            # 多出来的尾部值没有列名可挂，丢掉——DictReader 把它们塞进
            # key=None 的列表里，下游同样一次都没读过。
            values = {name: row[i] if i < len(row) else None for i, name in enumerate(header)}
            yield values


def _select_xlsx_sheet(workbook, sheet: str | int | None):
    """选不中就报错，绝不回落到第一张表——回落会让用户拿到一份完全不相干
    的数据，而且不报错。"""
    if sheet is None:
        return workbook.worksheets[0]
    if isinstance(sheet, int):
        if sheet >= len(workbook.worksheets):
            raise RowProcessingError(
                f"工作表序号 {sheet} 超出范围：这个文件只有 {len(workbook.worksheets)} 张表"
            )
        return workbook.worksheets[sheet]
    if sheet not in workbook.sheetnames:
        raise RowProcessingError(
            f"找不到名为 {sheet!r} 的工作表，这个文件里有：{workbook.sheetnames}"
        )
    return workbook[sheet]


def _read_xlsx_rows(path: Path, *, options: SourceParseOptions) -> Iterator[dict[str, str]]:
    """流式读取 xlsx 的指定工作表与表头行。read_only=True 让 openpyxl 用
    懒加载模式逐行产出，不把整个工作表读进内存，跟 CSV 路径同一个"18 万+
    行不能爆内存"的约束。data_only=True 拿单元格公式算出来的值，不拿公式
    字符串本身。

    工作表和表头行曾经写死成"第一个 sheet、第一行"，见
    docs/superpowers/specs/2026-09-15-source-parse-options-design.md
    ——那个决策在这里被推翻。"""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = _select_xlsx_sheet(workbook, options.sheet)
        rows_iter = worksheet.iter_rows(values_only=True)
        header_cells: tuple | None = None
        for row_number, row in enumerate(rows_iter, start=1):
            if row_number == options.header_row:
                header_cells = row
                break
        if header_cells is None:
            return
        header = header_from(
            [str(cell).strip() if cell is not None else "" for cell in header_cells]
        )
        first_data_row = options.resolved_first_data_row
        for row_number, row in enumerate(rows_iter, start=options.header_row + 1):
            if row_number < first_data_row:
                continue
            values = {
                header[i]: convert_excel_cell_to_string(row[i] if i < len(row) else None)
                for i in range(len(header))
            }
            # openpyxl 的 read_only 迭代会按工作表"已用范围"补齐行数，哪怕
            # 某一行早就被清空也会产出全空的幽灵行（常见于手工编辑过的
            # Excel 导出文件）。跳过全空行，避免这些幽灵行被当成"缺列"的
            # 脏数据行计入 skipped_rows，也让这条路径跟 CSV 侧对空行的
            # 处理保持一致。
            if not any(values.values()):
                continue
            yield values
    finally:
        workbook.close()


def _xlrd_cell_to_python_value(cell: "xlrd.sheet.Cell", datemode: int) -> object:
    """把 xlrd 的 Cell（用 ctype 标记类型、日期存成 Excel 序列号）归一化成
    openpyxl 风格的原生 Python 值（int/float/str/bool/datetime/None），
    这样就能复用同一个 convert_excel_cell_to_string 做字符串化，不用给
    xlrd 单独写一套转换规则。"""
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        # 公式错误单元格（#DIV/0!、#N/A 等）——cell.value 是一个内部错误码
        # 整数，直接透传会被字符串化成一个看起来正常、实际语义错误的值。
        # 当成空值处理，让这一行走"缺列"的正常脏数据路径。
        return None
    return cell.value  # XL_CELL_NUMBER（float）/ XL_CELL_TEXT（str）


def _select_xls_sheet(workbook: "xlrd.book.Book", sheet: str | int | None):
    """理由同 _select_xlsx_sheet：选不中就报错，不回落。"""
    if sheet is None:
        return workbook.sheet_by_index(0)
    if isinstance(sheet, int):
        if sheet >= workbook.nsheets:
            raise RowProcessingError(
                f"工作表序号 {sheet} 超出范围：这个文件只有 {workbook.nsheets} 张表"
            )
        return workbook.sheet_by_index(sheet)
    if sheet not in workbook.sheet_names():
        raise RowProcessingError(
            f"找不到名为 {sheet!r} 的工作表，这个文件里有：{workbook.sheet_names()}"
        )
    return workbook.sheet_by_name(sheet)


def _read_xls_rows(path: Path, *, options: SourceParseOptions) -> Iterator[dict[str, str]]:
    """读取旧版二进制 xls 的指定工作表与表头行。xlrd 没有 openpyxl 那种
    懒加载流式模式，会把整个工作表读进内存——xls 是被淘汰的旧格式，体量
    通常不大，这里不为了流式特意做额外处理。"""
    workbook = xlrd.open_workbook(str(path))
    worksheet = _select_xls_sheet(workbook, options.sheet)
    header_idx = options.header_row - 1
    if worksheet.nrows <= header_idx:
        return
    header = header_from(
        [str(worksheet.cell_value(header_idx, col)).strip() for col in range(worksheet.ncols)]
    )
    for row_idx in range(options.resolved_first_data_row - 1, worksheet.nrows):
        values = {
            header[col_idx]: convert_excel_cell_to_string(
                _xlrd_cell_to_python_value(worksheet.cell(row_idx, col_idx), workbook.datemode)
            )
            for col_idx in range(len(header))
        }
        # 跟 _read_xlsx_rows 同样的理由：跳过全空行。
        if not any(values.values()):
            continue
        yield values


def read_table_rows(
    path: Path, options: SourceParseOptions | None = None
) -> Iterator[dict[str, str]]:
    """按扩展名分流到对应的行读取器，统一产出 dict[str, str]。

    这是 ETL 三层管道的第一层（staging）的唯一入口：解析 + 类型归一，
    不认识 node_key、不认识本体，只把各种格式的表统一成行序列。见
    docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md。

    options 省略时的行为跟解析选项引入之前完全一致（第一个工作表、第一行
    表头）——存量映射里没有 sources 段，全靠这条保持不变。
    """
    opts = options if options is not None else SourceParseOptions()
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        if opts.sheet is not None:
            # 安静忽略会让用户以为自己选中了某张表。
            raise RowProcessingError(
                f"{suffix} 文件没有工作表的概念，不能指定 sheet：{path.name}"
            )
        delimiter = "," if suffix == ".csv" else "\t"
        yield from _read_delimited_rows(path, delimiter=delimiter, options=opts)
    elif suffix == ".xlsx":
        yield from _read_xlsx_rows(path, options=opts)
    elif suffix == ".xls":
        yield from _read_xls_rows(path, options=opts)
    else:
        raise RowProcessingError(f"不支持的数据文件类型: {suffix!r}（{path.name}）")
