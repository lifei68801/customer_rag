import csv

from app.ingestion.ticket_parser import TicketColumnMapping, parse_ticket_csv


def _write_csv(path, rows: list[dict], *, fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_parses_resolved_ticket_into_a_chunk_combining_question_and_resolution(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "ticket_id": "T1001",
                "subject": "登录失败",
                "description": "输入正确密码仍提示登录失败",
                "resolution": "清除浏览器缓存后重新登录即可解决。",
                "status": "resolved",
            }
        ],
        fieldnames=["ticket_id", "subject", "description", "resolution", "status"],
    )

    chunks = parse_ticket_csv(csv_path)

    assert len(chunks) == 1
    assert "登录失败" in chunks[0].text
    assert "输入正确密码仍提示登录失败" in chunks[0].text
    assert "清除浏览器缓存后重新登录即可解决。" in chunks[0].text
    assert chunks[0].heading_path == ["工单T1001"]
    assert chunks[0].source == str(csv_path)


def test_skips_tickets_without_resolution_by_default(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "ticket_id": "T1002",
                "subject": "网络连不上",
                "description": "",
                "resolution": "",
                "status": "open",
            }
        ],
        fieldnames=["ticket_id", "subject", "description", "resolution", "status"],
    )

    chunks = parse_ticket_csv(csv_path)

    assert chunks == []


def test_includes_unresolved_tickets_when_resolved_only_is_false(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "ticket_id": "T1002",
                "subject": "网络连不上",
                "description": "",
                "resolution": "",
                "status": "open",
            }
        ],
        fieldnames=["ticket_id", "subject", "description", "resolution", "status"],
    )

    chunks = parse_ticket_csv(csv_path, resolved_only=False)

    assert len(chunks) == 1
    assert "网络连不上" in chunks[0].text


def test_supports_custom_column_mapping_for_different_ticketing_systems(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "工单编号": "W2001",
                "标题": "打印机无法连接",
                "问题描述": "打印机显示离线状态",
                "处理方案": "重启打印机并重新连接WiFi。",
            }
        ],
        fieldnames=["工单编号", "标题", "问题描述", "处理方案"],
    )

    mapping = TicketColumnMapping(
        ticket_id="工单编号",
        subject="标题",
        description="问题描述",
        resolution="处理方案",
    )

    chunks = parse_ticket_csv(csv_path, column_mapping=mapping)

    assert len(chunks) == 1
    assert "打印机无法连接" in chunks[0].text
    assert "重启打印机并重新连接WiFi。" in chunks[0].text
    assert chunks[0].heading_path == ["工单W2001"]


def test_handles_missing_optional_description_column_gracefully(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "ticket_id": "T1003",
                "subject": "账号被锁定",
                "resolution": "联系管理员解锁账号。",
            }
        ],
        fieldnames=["ticket_id", "subject", "resolution"],
    )

    chunks = parse_ticket_csv(csv_path)

    assert len(chunks) == 1
    assert "账号被锁定" in chunks[0].text
    assert "联系管理员解锁账号。" in chunks[0].text


def test_skips_ticket_without_ticket_id_uses_generic_heading(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {
                "ticket_id": "",
                "subject": "支付失败",
                "resolution": "检查银行卡余额是否充足。",
            }
        ],
        fieldnames=["ticket_id", "subject", "resolution"],
    )

    chunks = parse_ticket_csv(csv_path)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["历史工单"]


def test_multiple_tickets_produce_multiple_chunks(tmp_path):
    csv_path = tmp_path / "tickets.csv"
    _write_csv(
        csv_path,
        [
            {"ticket_id": "T1", "subject": "问题A", "resolution": "答案A"},
            {"ticket_id": "T2", "subject": "问题B", "resolution": "答案B"},
        ],
        fieldnames=["ticket_id", "subject", "resolution"],
    )

    chunks = parse_ticket_csv(csv_path)

    assert len(chunks) == 2
    assert "问题A" in chunks[0].text
    assert "问题B" in chunks[1].text
