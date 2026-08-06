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
    # 部分供应商（如阿里百炼）单次 embeddings 请求最多接受的文本条数有硬
    # 限制，超过直接 400；不设置则一次性发全部文本（兼容没有这类限制的
    # 供应商）。
    embedding_batch_size: int | None = None

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

    # GraphRAG 人工待审核队列存储，SQLite 文件路径。
    graph_review_db_path: str = "data/graph_review_queue.sqlite3"

    # 对话记忆存储（会话滑窗+长期记忆条目），SQLite 文件路径。
    memory_db_path: str = "data/memory.sqlite3"

    # 增量摄取的变更追踪+任务队列存储，SQLite 文件路径。
    ingestion_db_path: str = "data/ingestion.sqlite3"

    # 语音：ASR/TTS 均为可选项，三项配置任一缺失则对应功能不可用。
    asr_base_url: str | None = None
    asr_api_key: str | None = None
    asr_model: str | None = None

    tts_base_url: str | None = None
    tts_api_key: str | None = None
    tts_model: str | None = None
    # 阿里百炼 CosyVoice 系列 TTS 专用配置（走 dashscope SDK 的 WebSocket
    # 协议，不是 OpenAI 兼容 REST 接口，见 dashscope_tts.py）。三项都设置
    # 时优先于上面的 tts_base_url 通用 provider；voice 必须是提前用
    # dashscope VoiceEnrollmentService 克隆好的 voice_id，标准音色名在
    # 私有部署端点上不可用（实测报 InvalidParameter）。
    tts_dashscope_websocket_url: str | None = None
    tts_dashscope_http_url: str | None = None
    tts_dashscope_voice: str | None = None

    # Agent 自主规划（Planner<->ToolCall 循环）总开关，默认关闭——关闭时用
    # 确定性检索路径，是 Planner 路径出问题时的回退（见
    # docs/AGENT_PLANNER_DESIGN.md）。语音请求无论这里怎么配置都强制走
    # 确定性路径，避免多轮 LLM 往返和首包延迟的硬性要求冲突。
    agent_enable_autonomous_planning: bool = False
    agent_max_tool_call_rounds: int = 3
    # 真实向量库几乎总能返回 Top-K 个最近邻，哪怕语义上完全不相关；设置后，
    # 检索到的记录即使非空，最高相关性分数低于这个阈值也会转人工工单，而不
    # 是把不相关资料硬塞给 LLM。默认不设置（None），行为与之前完全一致——
    # 具体阈值需要结合实际 embedding 模型/语料标定，不能瞎猜一个通用值。
    agent_min_relevance_score: float | None = None
