from app.api.admin_session import AdminSessionStore


def _create(store: AdminSessionStore, **overrides) -> str:
    kwargs = {"username": "admin", "role": "admin", "tenant_id": None}
    kwargs.update(overrides)
    return store.create_session(**kwargs)


def test_session_carries_identity():
    """session 必须记住"你是谁、属于哪个租户"。只记过期时间的话，每个
    请求都无从知道这两件事——租户隔离没法做，正是因为这个。"""
    store = AdminSessionStore()

    token = _create(store, username="alice", role="member", tenant_id="demo")

    session = store.get_session(token)
    assert session is not None
    assert session.username == "alice"
    assert session.role == "member"
    assert session.tenant_id == "demo"


def test_admin_session_has_no_tenant():
    """admin 不属于任何租户。给它绑一个会让"admin 能看所有租户"变得含糊。"""
    store = AdminSessionStore()

    token = _create(store)

    assert store.get_session(token).tenant_id is None


def test_get_unknown_token_returns_none():
    assert AdminSessionStore().get_session("not-a-real-token") is None


def test_get_expired_token_returns_none():
    store = AdminSessionStore()
    token = _create(store, ttl_seconds=-1)  # 立即过期

    assert store.get_session(token) is None


def test_revoke_session_invalidates_the_token():
    store = AdminSessionStore()
    token = _create(store)

    store.revoke_session(token)

    assert store.get_session(token) is None


def test_create_session_sweeps_expired_entries_without_needing_a_read():
    store = AdminSessionStore()
    expired_token = _create(store, ttl_seconds=-1)
    # 直接查内部字典，不经过 get_session（get_session 自己也会清理，这里
    # 要证明的是 create_session 本身的顺手清理，两者不能互相掩盖）
    assert expired_token in store._sessions

    _create(store)

    assert expired_token not in store._sessions


def test_sessions_do_not_leak_between_users():
    """两个人各自的 session 是各自的。共用一份状态的话，后登录的会顶掉
    前一个的身份——而那是静默的越权。"""
    store = AdminSessionStore()

    admin_token = _create(store, username="admin", role="admin", tenant_id=None)
    member_token = _create(store, username="alice", role="member", tenant_id="demo")

    assert store.get_session(admin_token).username == "admin"
    assert store.get_session(member_token).username == "alice"
    assert store.get_session(admin_token).role == "admin"


def test_set_current_tenant_replaces_the_frozen_session_in_place():
    """AdminSession 是 frozen dataclass，改不了字段——必须整条替换。

    直接赋值会抛 FrozenInstanceError；而如果实现是"新建一条但没写回字典"，
    下一次 get_session 拿到的还是旧值，界面上表现为"切了租户但没切"。
    """
    store = AdminSessionStore()
    token = store.create_session(username="admin", role="admin", tenant_id=None)

    updated = store.set_current_tenant(token, "acme")

    assert updated is not None
    assert updated.current_tenant_id == "acme"
    assert store.get_session(token).current_tenant_id == "acme"


def test_set_current_tenant_returns_none_for_unknown_token():
    store = AdminSessionStore()
    assert store.set_current_tenant("no-such-token", "acme") is None


def test_new_session_starts_with_member_tenant_as_current():
    """member 绑定一个租户，登录后当前租户就该是它，不该是 None——
    否则前台第一次问答时没有租户可用。"""
    store = AdminSessionStore()
    token = store.create_session(username="alice", role="member", tenant_id="demo")
    assert store.get_session(token).current_tenant_id == "demo"
