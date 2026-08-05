from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_", env_file=".env")

    llm_base_url: str
    llm_api_key: str
    llm_model: str

    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimension: int

    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "faq_chunks"

    # Rerank 为可选项：不配置时 /qa 直接跳过精排，仅走 RRF 融合排序。
    rerank_base_url: str | None = None
    rerank_api_key: str | None = None
    rerank_model: str | None = None

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme123"
    # 默认指向占位示例数据，正式环境必须替换为真实术语表文件路径。
    terminology_path: str = "app/graphrag/terminology_seed.yaml"

    # 对话记忆存储（会话滑窗+长期记忆条目），SQLite 文件路径。
    memory_db_path: str = "data/memory.sqlite3"

    # 语音：ASR/TTS 均为可选项，三项配置任一缺失则对应功能不可用。
    asr_base_url: str | None = None
    asr_api_key: str | None = None
    asr_model: str | None = None

    tts_base_url: str | None = None
    tts_api_key: str | None = None
    tts_model: str | None = None
