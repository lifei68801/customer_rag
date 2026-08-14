from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_constraints import add_allowed_combination, list_allowed_combinations
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
    is_ontology_confirmed,
)
from app.graphrag.ontology_relations import create_relation_type, list_relation_types

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def test_checkout_draft_seeds_defaults_for_brand_new_tenant():
    conn = await _conn()

    await checkout_draft(conn, "t1")

    result = await list_relation_types(conn, "t1", status="draft")
    assert len(result) == 10


async def test_is_ontology_confirmed_false_before_first_confirm():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    assert await is_ontology_confirmed(conn, "t1") is False


async def test_confirm_ontology_promotes_draft_to_confirmed():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x")

    await confirm_ontology(conn, "t1")

    confirmed = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed) == 11
    assert await is_ontology_confirmed(conn, "t1") is True


async def test_confirm_ontology_clears_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    await confirm_ontology(conn, "t1")

    assert await list_relation_types(conn, "t1", status="draft") == []


async def test_confirm_ontology_replaces_previous_confirmed_version():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")
    await checkout_draft(conn, "t1")
    from app.graphrag.ontology_relations import delete_relation_type
    await delete_relation_type(conn, "t1", "PRECEDES")

    await confirm_ontology(conn, "t1")

    confirmed = {r.relation_type for r in await list_relation_types(conn, "t1", status="confirmed")}
    assert "PRECEDES" not in confirmed
    assert len(confirmed) == 9


async def test_checkout_draft_after_confirm_copies_confirmed_into_new_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 10


async def test_checkout_draft_is_idempotent_when_draft_already_exists():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="CUSTOM", example_phrase="x")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 11


async def test_confirm_ontology_promotes_constraints_too():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    await confirm_ontology(conn, "t1")

    confirmed = await list_allowed_combinations(conn, "t1", status="confirmed")
    assert confirmed == [
        __import__("app.graphrag.ontology_constraints", fromlist=["AllowedCombination"]).AllowedCombination(
            subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店"
        )
    ]


async def test_confirm_ontology_is_idempotent_no_op_without_draft():
    """Regression test: confirm called without draft should be a no-op, not data loss."""
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    confirmed_after_first = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after_first) == 10

    # Second confirm without checkout should be a no-op
    await confirm_ontology(conn, "t1")

    confirmed_after_second = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after_second) == 10
    assert confirmed_after_second == confirmed_after_first


async def test_confirm_ontology_with_no_draft_does_not_delete_confirmed():
    """Regression test: confirm on a tenant with only confirmed data should not wipe it."""
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    # Verify 10 confirmed rows exist
    confirmed = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed) == 10

    # Call confirm again without any draft
    await confirm_ontology(conn, "t1")

    # Confirmed data should still be intact
    confirmed_after = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed_after) == 10
    assert {r.relation_type for r in confirmed_after} == {r.relation_type for r in confirmed}
