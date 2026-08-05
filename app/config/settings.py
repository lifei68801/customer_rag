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
