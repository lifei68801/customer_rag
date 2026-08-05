import aiosqlite

from app.memory.customer_profile import (
    ensure_customer_profile_schema,
    get_customer_profile,
    upsert_customer_profile,
)


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_customer_profile_schema(conn)
    return conn


async def test_get_returns_default_profile_when_never_set():
    conn = await _connect()

    profile = await get_customer_profile(conn, tenant_id="t1", customer_id="c1")

    assert profile["is_vip"] is False
    assert profile["feedback_label"] == "neutral"
    assert profile["communication_style"] == "formal"


async def test_upsert_then_get_returns_the_stored_profile():
    conn = await _connect()

    await upsert_customer_profile(
        conn,
        tenant_id="t1",
        customer_id="c1",
        is_vip=True,
        feedback_label="too_proactive",
        communication_style="casual",
    )

    profile = await get_customer_profile(conn, tenant_id="t1", customer_id="c1")

    assert profile["is_vip"] is True
    assert profile["feedback_label"] == "too_proactive"
    assert profile["communication_style"] == "casual"


async def test_upsert_overwrites_previous_values():
    conn = await _connect()
    await upsert_customer_profile(
        conn, tenant_id="t1", customer_id="c1", is_vip=False,
        feedback_label="neutral", communication_style="formal",
    )

    await upsert_customer_profile(
        conn, tenant_id="t1", customer_id="c1", is_vip=True,
        feedback_label="more_proactive", communication_style="casual",
    )

    profile = await get_customer_profile(conn, tenant_id="t1", customer_id="c1")
    assert profile["feedback_label"] == "more_proactive"


async def test_profiles_scoped_to_tenant():
    conn = await _connect()
    await upsert_customer_profile(
        conn, tenant_id="t1", customer_id="c1", is_vip=True,
        feedback_label="neutral", communication_style="formal",
    )

    profile = await get_customer_profile(conn, tenant_id="t2", customer_id="c1")

    assert profile["is_vip"] is False
