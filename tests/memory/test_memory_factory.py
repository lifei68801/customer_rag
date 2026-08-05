from app.config.settings import Settings
from app.memory.factory import build_memory_conn_from_settings
from app.memory.memory_store import upsert_memory_item, list_active_memory_items


async def test_build_memory_conn_from_settings_creates_usable_schema(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    settings = Settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
        memory_db_path=str(db_path),
    )

    conn = await build_memory_conn_from_settings(settings)
    await upsert_memory_item(conn, memory_id="m1", user_id="u1", text="测试记忆")
    items = await list_active_memory_items(conn, user_id="u1")

    assert [i["text"] for i in items] == ["测试记忆"]
    assert db_path.exists()
