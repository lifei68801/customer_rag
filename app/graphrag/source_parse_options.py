"""staging 层的解析选项：一张表该怎么读。

这里回答的是"这张表怎么读"，不是"读出来的列怎么映射到本体"——后者是
projection 层和本体的事。今天这两件事挤在同一步里，正是 MUJI 那张
113 列、四层表头的 SKU 主数据表暴露出来的问题。

见 docs/superpowers/specs/2026-09-15-source-parse-options-design.md。
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidSourceParseOptionsError(Exception):
    """解析选项自身不合法——表头行号小于 1、首数据行不在表头之后等。"""


@dataclass(frozen=True)
class SourceParseOptions:
    """缺省值必须等于"本选项引入之前的行为"：第一个工作表、第一行表头。

    存量的映射里没有 sources 段，会按缺省解释；任何一个缺省值变了，
    所有存量配置的行为都会跟着变，而没有人会收到通知。
    """

    #: 工作表名或 0-based 序号；None = 第一个。csv/tsv 必须是 None。
    #: 两种都收：名字可读、能自解释，但会被重命名；序号稳定、但看不出是哪张。
    sheet: str | int | None = None
    #: 1-based，跟 Excel 的行号对齐。用户是对着 Excel 看的。
    header_row: int = 1
    #: 1-based；None = header_row + 1。
    #:
    #: 独立字段而不是算出来的，是因为表头和数据之间可能夹着说明行——MUJI
    #: 那张表第 5 行是"Character Limit"，用户若选第 3 行的英文名当表头，
    #: 中间三行都得跳过。
    first_data_row: int | None = None

    def __post_init__(self) -> None:
        if self.header_row < 1:
            raise InvalidSourceParseOptionsError(
                f"header_row 必须从 1 开始（跟 Excel 行号一致），收到 {self.header_row}"
            )
        if self.first_data_row is not None and self.first_data_row <= self.header_row:
            raise InvalidSourceParseOptionsError(
                f"first_data_row（{self.first_data_row}）必须大于 header_row（{self.header_row}）"
            )
        if isinstance(self.sheet, int) and self.sheet < 0:
            raise InvalidSourceParseOptionsError(f"sheet 序号不能是负数，收到 {self.sheet}")

    @property
    def resolved_first_data_row(self) -> int:
        """紧跟表头是绝大多数表的形状，所以 first_data_row 缺省可省。"""
        return self.first_data_row if self.first_data_row is not None else self.header_row + 1
