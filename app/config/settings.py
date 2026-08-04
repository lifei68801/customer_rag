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
