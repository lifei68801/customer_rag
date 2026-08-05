from app.config.settings import Settings
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.review_queue import enqueue_for_review, list_pending_reviews


async def test_build_review_conn_from_settings_creates_usable_schema(tmp_path):
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = Settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
        graph_review_db_path=str(db_path),
    )

    conn = await build_review_conn_from_settings(settings)
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )
    pending = await list_pending_reviews(conn)

    assert len(pending) == 1
    assert db_path.exists()
