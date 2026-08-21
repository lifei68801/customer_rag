from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.etl_runs_store import (
    create_etl_run,
    ensure_etl_runs_schema,
    get_etl_run,
    list_etl_runs,
)
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app


class _FakeGraphClient:
    """占位图谱客户端——本文件里的用例只用空 entities/relations 的配置跑批，
    不会真的调用 sync_term/merge_relation，这里保留桩方法只是让
    deps.get_graph_client 的依赖注入能满足类型，不需要记录调用。"""

    async def sync_term(self, term) -> None:
        pass

    async def merge_relation(self, **kwargs) -> None:
        pass


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_etl_runs_schema(conn)
    # Task 4：start_schema_etl_run 现在会先用 review_conn 调
    # require_active_tenant() 校验 tenant_id——真实的 deps.get_review_conn()
    # 会自动建好 tenants 表并回填历史租户，这里是手工建表的测试连接，绕开了
    # 那条路径，必须显式建表 + 注册本文件用例里出现过的 tenant_id。
    # "unconfirmed_tenant" 也要注册成 active：它是用来验证"schema 未确认
    # 时返回 400"这条业务规则的，必须先通过租户存在性校验才能走到那条
    # 业务检查，否则会被新加的校验提前拦成 404，测试的原意就测不到了。
    await create_tenants_table(conn)
    for _tid in ("muji", "unconfirmed_tenant"):
        await create_tenant(conn, tenant_id=_tid, name=_tid)
    return conn


@pytest.fixture
def review_conn():
    """跟本文件其它 review_conn 风格的 fixture 一样，需要显式 close：aiosqlite
    的后台工作线程不是 daemon 线程，泄漏未关闭的连接会让 pytest 进程卡在
    解释器退出阶段。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


@pytest.fixture
def client(review_conn, tmp_path):
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    app.dependency_overrides[deps.get_upload_dir] = lambda: tmp_path / "uploads"
    yield TestClient(app)
    app.dependency_overrides.clear()


async def _confirm_muji_schema(review_conn: aiosqlite.Connection) -> None:
    await create_term_type(review_conn, tenant_id="muji", value="Product")
    await checkout_draft(review_conn, "muji")
    await confirm_ontology(review_conn, "muji")


def test_status_returns_false_when_schema_not_confirmed(client):
    response = client.get("/api/admin/unconfirmed_tenant/schema-etl/status")
    assert response.status_code == 200
    assert response.json() == {"ontology_confirmed": False}


def test_status_returns_true_after_confirm(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))

    response = client.get("/api/admin/muji/schema-etl/status")

    assert response.json() == {"ontology_confirmed": True}


def test_start_run_returns_404_for_unknown_tenant(client):
    """Task 4：租户存在性校验要先于"schema 是否确认"这条业务规则生效——
    一个从未在 tenants 注册表里登记过的 tenant_id 应该直接 404，而不是被
    当作合法租户走到"未确认"的 400。"""
    files = {"config": ("config.yaml", b"tenant_id: no-such-tenant\nentities: []\nrelations: []\n")}
    response = client.post("/api/admin/no-such-tenant/schema-etl/runs", files=files)
    assert response.status_code == 404


def test_start_run_rejects_when_schema_not_confirmed(client):
    files = {"config": ("config.yaml", b"tenant_id: unconfirmed_tenant\nentities: []\nrelations: []\n")}
    response = client.post("/api/admin/unconfirmed_tenant/schema-etl/runs", files=files)
    assert response.status_code == 400


def test_start_run_returns_run_id_and_second_concurrent_start_is_rejected(client, review_conn):
    """TestClient 会在 .post() 返回之前就把 BackgroundTasks 跑完（见
    tests/api/test_admin_document_routes.py 里同款说明），所以这条用例没法
    靠真的并发请求撞见 409——这里改为直接模拟"第一条跑批仍在 running"的
    并发窗口：先调用 create_etl_run 占住 running 状态（不经过 /runs 路由，
    避免触发它自带的 background_tasks 把这条记录立刻跑完），再发起
    /runs 请求，验证路由把 EtlRunAlreadyRunningError 正确映射成 409。
    路由自己"正常情况下返回 run_id"这一半，由本文件另一个用例
    （test_start_run_returns_run_id_when_no_run_in_progress）覆盖。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    asyncio.run(
        create_etl_run(review_conn, run_id="already-running", tenant_id="muji", started_at="2026-08-17T10:00:00")
    )

    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}
    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 409


def test_start_run_returns_run_id_when_no_run_in_progress(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert run_id

    # BackgroundTasks 在 .post() 返回之前就跑完了（见上面用例的说明），
    # 所以这里能直接断言跑批已经落到终态，而不是还停在 running。
    detail = asyncio.run(get_etl_run(review_conn, tenant_id="muji", run_id=run_id))
    assert detail.status == "completed"


def test_start_run_rejects_config_whose_tenant_id_differs_from_path(client, review_conn):
    """run_schema_etl 真正写数据时用的是 YAML 里的 tenant_id，而并发防护、
    schema 预检查、历史归属用的是 URL 路径上的 tenant_id——两者不一致时，
    往多个不同路径 tenant 提交同一份 YAML 就能绕开"每租户同时只有一次跑批"
    的部分唯一索引。这里验证路由在落库前就把不一致挡成 400。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"tenant_id: other_tenant\nentities: []\nrelations: []\n")}

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "other_tenant" in detail and "muji" in detail
    assert asyncio.run(list_etl_runs(review_conn, "muji")) == []


def test_start_run_rejects_malformed_config_with_400(client, review_conn):
    """格式非法的 YAML（这里缺 tenant_id）应该在入口就变成 400，而不是先
    创建一条 running 记录、再由后台任务把它标成 failed。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"entities: []\nrelations: []\n")}

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 400
    assert "解析失败" in response.json()["detail"]
    assert asyncio.run(list_etl_runs(review_conn, "muji")) == []


def test_start_run_cleans_up_uploaded_files_when_rejected_with_409(client, review_conn, tmp_path):
    """被 409 拒绝的请求已经把 config/CSV 落盘了，但没有任何 etl_runs 记录
    引用它们——不清理的话这些文件永远留在磁盘上没人认领。run_id 是请求内部
    生成的 uuid，测试无法预知，所以改为快照对比目录列表：拒绝前后
    schema-etl/{tenant}/ 下不应多出任何子目录。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    asyncio.run(
        create_etl_run(review_conn, run_id="already-running", tenant_id="muji", started_at="2026-08-17T10:00:00")
    )
    tenant_dir = tmp_path / "uploads" / "schema-etl" / "muji"
    before = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []

    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("products.csv", b"code,name\n")),
    ]
    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 409
    after = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []
    assert after == before


def test_start_run_sanitizes_dotdot_data_filename_and_stays_inside_run_dir(client, review_conn, tmp_path):
    """回归测试：_sanitize_data_filename 的正则只剥路径分隔符，纯 ".."
    文件名穿透正则原样存活——run_dir / ".." 指向 run_dir 的父目录（这个
    父目录必然已存在，是 run_dir.mkdir(parents=True) 顺带建出来的），
    对着一个已存在的目录 write_bytes() 会抛 IsADirectoryError，变成一个
    未捕获的 500，而不是"逃出 run_dir 写坏东西"式的任意路径穿越。这里
    验证：(1) 请求不再 500；(2) 落盘的每个文件都真的在 run_dir 内部，
    不是靠"没报错"就断言过关。使用 ..data.csv 作为测试文件名（有有效扩展名），
    它会被 _sanitize_data_filename 转换成 __data.csv，仍然测试了文件名消毒逻辑。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("..data.csv", b"attempted traversal payload")),
    ]

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    run_dir = (tmp_path / "uploads" / "schema-etl" / "muji" / run_id).resolve()
    assert run_dir.is_dir()

    written_files = [p for p in run_dir.iterdir() if p.is_file()]
    assert written_files, "消毒后的 data_files 文件应该落在 run_dir 里"
    for path in written_files:
        assert path.resolve().is_relative_to(run_dir)

    # run_dir 的父目录（.../schema-etl/muji）除了 run_id 自己这一个子目录，
    # 不应该被写入任何东西——证明没有文件溢出到 run_dir 之外。
    parent = run_dir.parent
    assert [child.name for child in parent.iterdir()] == [run_id]


def test_get_run_not_found_returns_404(client):
    response = client.get("/api/admin/muji/schema-etl/runs/nonexistent")
    assert response.status_code == 404


def test_list_runs_empty_for_new_tenant(client):
    response = client.get("/api/admin/muji/schema-etl/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_list_runs_returns_started_run(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}
    start_response = client.post("/api/admin/muji/schema-etl/runs", files=files)
    run_id = start_response.json()["run_id"]

    response = client.get("/api/admin/muji/schema-etl/runs")

    assert response.status_code == 200
    assert [r["run_id"] for r in response.json()["runs"]] == [run_id]


def test_get_run_returns_completed_detail(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}
    start_response = client.post("/api/admin/muji/schema-etl/runs", files=files)
    run_id = start_response.json()["run_id"]

    response = client.get(f"/api/admin/muji/schema-etl/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["report"]["entities_written"] == 0


def test_report_csv_returns_404_when_run_not_found(client):
    response = client.get("/api/admin/muji/schema-etl/runs/nonexistent/report.csv")
    assert response.status_code == 404


def test_report_csv_returns_csv_with_header_for_completed_run(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}
    start_response = client.post("/api/admin/muji/schema-etl/runs", files=files)
    run_id = start_response.json()["run_id"]

    response = client.get(f"/api/admin/muji/schema-etl/runs/{run_id}/report.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0] == "label,source_file,row_number,reason"


def test_get_sample_returns_400_when_ontology_not_confirmed():
    async def override_review_conn():
        conn = await _open_review_conn()
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 400
        assert "确认" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_sample_returns_400_when_schema_confirmed_but_has_no_term_types():
    async def override_review_conn():
        conn = await _open_review_conn()
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="SAMPLE_LINK", example_phrase="A SAMPLE_LINK B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 400
        assert "没有任何已确认的实体类型" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_sample_returns_files_with_config_yaml_first():
    async def override_review_conn():
        conn = await _open_review_conn()
        await create_term_type(conn, tenant_id="demo", value="商品")
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="SAMPLE_LINK", example_phrase="A SAMPLE_LINK B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 200
        data = response.json()
        assert data["files"][0]["filename"] == "config.yaml"
        assert any(f["filename"] == "商品.csv" for f in data["files"])
    finally:
        app.dependency_overrides.clear()


def test_download_sample_zip_returns_a_valid_zip_containing_the_same_files():
    import zipfile
    import io

    async def override_review_conn():
        conn = await _open_review_conn()
        await create_term_type(conn, tenant_id="demo", value="商品")
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="SAMPLE_LINK", example_phrase="A SAMPLE_LINK B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample.zip")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert "config.yaml" in archive.namelist()
        assert "商品.csv" in archive.namelist()
    finally:
        app.dependency_overrides.clear()


def test_start_run_rejects_unsupported_data_file_extension(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("report.pdf", b"%PDF-1.4 fake pdf content")),
    ]

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 400
    assert "report.pdf" in response.json()["detail"]
    assert "不支持的文件类型" in response.json()["detail"]


def test_start_run_rejects_unsupported_extension_cleans_up_run_dir(client, review_conn, tmp_path):
    """扩展名校验失败要清理已经创建的 run_dir，不能在磁盘上留下半成品目录
    ——跟本文件其它 400 分支（tenant_id 不一致、配置解析失败）的清理方式
    保持一致。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    tenant_dir = tmp_path / "uploads" / "schema-etl" / "muji"
    before = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("report.pdf", b"%PDF-1.4 fake pdf content")),
    ]

    client.post("/api/admin/muji/schema-etl/runs", files=files)

    after = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []
    assert after == before, "校验失败后不应该在 tenant 目录下留下新的 run_id 目录"


def test_start_run_accepts_xlsx_data_file(client, review_conn):
    """扩展名白名单要放行 xlsx，不能因为加了白名单反而把新支持的格式也
    挡在外面。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("products.xlsx", b"PK\x03\x04fake xlsx bytes")),
    ]

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 200
