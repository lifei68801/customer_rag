"""组织管理 + 账号租户授权的管理入口。

这两组端点分居两个文件（组织在 admin_org_routes.py，账号授权在既有的
admin_account_routes.py），但都要经过同一套"没有授权入口就配不出多数字人"
的验证，放在一个测试文件里覆盖，跟 task-7-brief.md 给的用例形状一致。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.auth.admin_users_store import create_admin_user
from app.graphrag.organizations_store import ensure_organizations_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table, set_tenant_status
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema

pytestmark = pytest.mark.anyio


async def _build_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_admin_auth_schema(conn)
    await create_tenants_table(conn)
    # 建完 tenants 表才能建 organizations——ensure_organizations_schema 要给
    # tenants 补 org_id 列（app/graphrag/organizations_store.py）。
    await ensure_organizations_schema(conn)
    await create_admin_user(conn, username="admin", password="password1", role="admin", tenant_id=None)
    await create_admin_user(conn, username="alice", password="password1", role="member", tenant_id="a")
    for tenant_id in ("a", "b", "c"):
        await create_tenant(conn, tenant_id=tenant_id, name=f"Tenant {tenant_id.upper()}")
    return conn


def _session(role: str, username: str) -> AdminSession:
    return AdminSession(username=username, role=role, tenant_id=None, expires_at=1e18)


@pytest.fixture
def org_conn():
    """一个装好 admin_users/user_tenants/organizations/tenants 四张表的本体
    库连接，预置一个 admin（admin）、一个 member（alice，默认租户 a）和
    三个启用中的租户（a/b/c）。默认以 admin 身份发请求；单条用例要以别的
    身份发，调 _as(role, username) 临时切换。
    """
    conn = asyncio.run(_build_conn())
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.require_admin_session] = lambda: _session("admin", "admin")
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(deps.get_review_conn, None)
        app.dependency_overrides.pop(deps.require_admin_session, None)


def _as(role: str, username: str = "admin") -> None:
    app.dependency_overrides[deps.require_admin_session] = lambda: _session(role, username)


def _client() -> TestClient:
    return TestClient(app)


def _post_org(org_conn, *, org_id: str = "org1", name: str = "Acme", role: str = "admin"):
    _as(role)
    return _client().post("/api/admin/organizations", json={"org_id": org_id, "name": name})


def _get_orgs(org_conn):
    _as("admin")
    return _client().get("/api/admin/organizations")


def _assign_tenant_to_org(org_conn, tenant_id: str, org_id: str | None):
    """把租户挂到组织下。这个端点实际实现在 admin_tenant_routes.py（改的
    是一行 tenants 记录），不在 admin_org_routes.py 里——但用例留在这个
    文件，因为它跟"建组织、列组织"是同一个业务动作链条的下一步，测的是
    通过公开 API 路径观察到的行为，不是"这个函数定义在哪个文件"。"""
    _as("admin")
    return _client().put(f"/api/admin/tenants/{tenant_id}/organization", json={"org_id": org_id})


def _put_account_tenants_raw(org_conn, username: str, tenant_ids: list[str]):
    _as("admin")
    return _client().put(
        f"/api/admin/accounts/{username}/tenants", json={"tenant_ids": tenant_ids}
    )


def _put_account_tenants(org_conn, username: str, tenant_ids: list[str]) -> list[str]:
    resp = _put_account_tenants_raw(org_conn, username, tenant_ids)
    assert resp.status_code == 200, resp.text
    return resp.json()["tenant_ids"]


def _get_account_tenants(org_conn, username: str) -> list[str]:
    _as("admin")
    resp = _client().get(f"/api/admin/accounts/{username}/tenants")
    assert resp.status_code == 200, resp.text
    return resp.json()["tenant_ids"]


def test_member_cannot_create_an_organization(org_conn):
    """组织管理是 admin 专属。member 能建组织的话，他就能把别人的租户
    挂到自己建的组织下——那是一条绕开 user_tenants 的路。"""
    assert _post_org(org_conn, role="member").status_code == 403


def test_admin_creates_an_org_and_lists_its_tenants(org_conn):
    resp = _post_org(org_conn, org_id="org1", name="Acme")
    assert resp.status_code == 201

    resp = _assign_tenant_to_org(org_conn, "a", "org1")
    assert resp.status_code == 200

    resp = _get_orgs(org_conn)
    assert resp.status_code == 200
    orgs = {o["org_id"]: o for o in resp.json()["organizations"]}
    assert orgs["org1"]["name"] == "Acme"
    assert orgs["org1"]["status"] == "active"
    assert orgs["org1"]["tenant_ids"] == ["a"]


def test_creating_a_duplicate_org_is_refused(org_conn):
    assert _post_org(org_conn, org_id="dup", name="First").status_code == 201
    resp = _post_org(org_conn, org_id="dup", name="Second")
    assert resp.status_code == 400


def test_assigning_a_tenant_to_a_nonexistent_org_is_refused(org_conn):
    resp = _assign_tenant_to_org(org_conn, "a", "不存在")
    assert resp.status_code == 400


def test_assigning_a_nonexistent_tenant_to_an_org_is_refused(org_conn):
    _post_org(org_conn, org_id="org1", name="Acme")
    resp = _assign_tenant_to_org(org_conn, "不存在", "org1")
    assert resp.status_code == 404


def test_putting_tenants_on_an_account_replaces_the_whole_set(org_conn):
    """全量替换而不是追加：界面上是一组复选框，用户取消勾选的那个必须
    真的被撤销。追加语义下"取消勾选"永远不生效，而界面看起来生效了。"""
    _put_account_tenants(org_conn, "alice", ["a", "b"])
    _put_account_tenants(org_conn, "alice", ["b", "c"])
    assert _get_account_tenants(org_conn, "alice") == ["b", "c"]


def test_granting_a_nonexistent_tenant_is_refused(org_conn):
    """授权一个不存在的租户要报错。放行的话，管理员以为自己给了权限，
    用户的右栏里却什么都没多——而没有任何地方说明为什么。"""
    assert _put_account_tenants_raw(org_conn, "alice", ["不存在"]).status_code == 400


def test_granting_to_a_nonexistent_account_is_refused(org_conn):
    """同理：拼错用户名不该静默成功。"""
    assert _put_account_tenants_raw(org_conn, "拼错了", ["a"]).status_code == 404


def test_admin_accounts_cannot_be_granted_tenants(org_conn):
    """admin 本来就不设限，给它「授权」是个无意义操作，而它会让管理员
    以为自己限制住了这个 admin——实际上没有。宁可报错也不要制造这个错觉。"""
    assert _put_account_tenants_raw(org_conn, "admin", ["a"]).status_code == 400


def test_putting_an_empty_list_is_refused(org_conn):
    """撤销到零和从未授权过在 user_tenants 表里长得一样：list_accessible_
    tenant_ids 只有在一条记录都没有时才回退到 admin_users.tenant_id。放行
    空列表的话，管理员以为收回了全部访问权，alice 却仍能进她的默认租户 a
    ——一次看不见的失败。彻底收回访问权限走停用账号，不是这个接口。"""
    _put_account_tenants(org_conn, "alice", ["a", "b"])
    resp = _put_account_tenants_raw(org_conn, "alice", [])
    assert resp.status_code == 400
    # 前一次调用留下的授权原封不动——先校验后写，不能因为这次请求非法就
    # 把已有的授权动了。
    assert _get_account_tenants(org_conn, "alice") == ["a", "b"]


def test_getting_tenants_for_a_nonexistent_account_is_refused(org_conn):
    _as("admin")
    resp = _client().get("/api/admin/accounts/拼错了/tenants")
    assert resp.status_code == 404


def test_saving_can_keep_an_existing_grant_on_a_now_disabled_tenant(org_conn):
    """2026-09-08 评审 Important 2：alice 被授权 [a, b]，b 之后停用。管理员
    打开账号页时前端复选框只列启用中的租户，看不到 b，但保存时如果原样
    带上 b（前端的职责，这里只测后端），后端不能把它当成"不存在或已停用"
    拒绝——那会让"保持一条指向已停用租户的授权不动"这个合法操作也做不到，
    管理员连补救都补不回去。"""
    _put_account_tenants(org_conn, "alice", ["a", "b"])
    asyncio.run(set_tenant_status(org_conn, "b", "disabled"))
    resp = _put_account_tenants_raw(org_conn, "alice", ["a", "b"])
    assert resp.status_code == 200, resp.text
    assert _get_account_tenants(org_conn, "alice") == ["a", "b"]


def test_granting_a_newly_disabled_tenant_the_account_never_had_is_still_refused(org_conn):
    """上一条测的是"保留已有授权"合法；这条测的是反面——停用期间的 b 从没
    授权给 alice 过，新授权它必须继续被拒。只并入 current（已有授权），
    不能松成"全部租户都算已知"，否则这条防线名存实亡。"""
    asyncio.run(set_tenant_status(org_conn, "b", "disabled"))
    resp = _put_account_tenants_raw(org_conn, "alice", ["a", "b"])
    assert resp.status_code == 400
    assert _get_account_tenants(org_conn, "alice") == []


def _disable_org(org_conn, org_id: str, *, role: str = "admin"):
    _as(role)
    return _client().post(f"/api/admin/organizations/{org_id}/disable")


def _enable_org(org_conn, org_id: str):
    _as("admin")
    return _client().post(f"/api/admin/organizations/{org_id}/enable")


def _delete_org(org_conn, org_id: str, *, role: str = "admin"):
    _as(role)
    return _client().delete(f"/api/admin/organizations/{org_id}")


def test_a_blank_org_id_is_refused_at_the_http_layer(org_conn):
    """空 org_id 要在接口这一层就被挡住。

    store 层的用例证明不了路由把那个异常翻成了 400——不翻的话它会冲成 500，
    响应体只有「Internal Server Error」，用户不知道是自己填错了。
    """
    resp = _post_org(org_conn, org_id="   ", name="Acme")

    assert resp.status_code == 400
    assert "组织 ID" in resp.text


def test_disabling_an_org_blocks_new_assignments(org_conn):
    """停用之后挂新租户返回 409，**且消息说清是"已停用"**。

    409 不是 400：不是请求写错了，是这个组织当前的状态不接收新租户——
    用户改请求体没用，得先去启用它。
    """
    _post_org(org_conn, org_id="org1", name="Acme")
    assert _assign_tenant_to_org(org_conn, "a", "org1").status_code == 200

    assert _disable_org(org_conn, "org1").status_code == 200

    resp = _assign_tenant_to_org(org_conn, "b", "org1")
    assert resp.status_code == 409
    assert "已停用" in resp.text
    # 已经挂着的那个不动——停用不是"把里面的东西倒出来"。
    body = _get_orgs(org_conn).json()["organizations"][0]
    assert body["tenant_ids"] == ["a"]
    assert body["status"] == "disabled"


def test_enabling_it_again_lets_assignments_through(org_conn):
    """反面：不重新启用的话，上一条在「一律 409」的实现下也是绿的。"""
    _post_org(org_conn, org_id="org1", name="Acme")
    _disable_org(org_conn, "org1")

    assert _enable_org(org_conn, "org1").status_code == 200

    assert _assign_tenant_to_org(org_conn, "a", "org1").status_code == 200


def test_a_tenant_can_still_leave_a_disabled_org(org_conn):
    """从停用的组织里移出去要放行。

    一并挡住的话，里面的租户就被锁死了：既挂不进新的，也出不来——而"先把
    租户移出去再删"正是删除那条路径要求用户做的事。
    """
    _post_org(org_conn, org_id="org1", name="Acme")
    _assign_tenant_to_org(org_conn, "a", "org1")
    _disable_org(org_conn, "org1")

    assert _assign_tenant_to_org(org_conn, "a", None).status_code == 200

    assert _get_orgs(org_conn).json()["organizations"][0]["tenant_ids"] == []


def test_deleting_an_empty_org_removes_it(org_conn):
    """建错了要能删掉——这是这一组功能存在的理由。"""
    _post_org(org_conn, org_id="typo", name="打错的")
    _post_org(org_conn, org_id="org1", name="Acme")

    assert _delete_org(org_conn, "typo").status_code == 200

    assert [o["org_id"] for o in _get_orgs(org_conn).json()["organizations"]] == ["org1"]


def test_deleting_an_org_that_still_has_tenants_is_refused(org_conn):
    """名下还有租户时 409，消息里说清还有几个，且组织和归属都没被动过。

    级联清掉的话，那些租户会静默地从组织视图里掉出来——用户不会注意到，
    直到打开看板发现分组变了。
    """
    _post_org(org_conn, org_id="org1", name="Acme")
    _assign_tenant_to_org(org_conn, "a", "org1")

    resp = _delete_org(org_conn, "org1")

    assert resp.status_code == 409
    assert "1" in resp.text
    body = _get_orgs(org_conn).json()["organizations"]
    assert [o["org_id"] for o in body] == ["org1"]
    assert body[0]["tenant_ids"] == ["a"]


def test_deleting_a_missing_org_is_a_404(org_conn):
    assert _delete_org(org_conn, "nope").status_code == 404


def test_member_cannot_disable_or_delete_an_organization(org_conn):
    """组织管理是 admin 专属，跟建组织同一档。"""
    _post_org(org_conn, org_id="org1", name="Acme")

    assert _disable_org(org_conn, "org1", role="member").status_code == 403
    assert _delete_org(org_conn, "org1", role="member").status_code == 403
