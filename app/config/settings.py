from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_LLM_", env_file=".env", extra="ignore")

    base_url: str
    api_key: str
    model: str
    # 部分推理模型（如 DeepSeek 的 v4-flash/v4-pro）默认会在 reasoning_content
    # 字段里先输出一大段隐藏思维链再给正式回答——2026-08-12 实测简单技术
    # 问题就有 47 秒纯思考、期间前端完全收不到任何输出（stream_complete()
    # 只转发 content，不转发 reasoning_content），表现为"迟迟不出字"。这里
    # 按 DeepSeek 请求体的 thinking.type 参数（实测 "disabled"/"enabled"
    # 两个值都生效，不传该字段时默认等价于 "enabled"）把开关做成配置项，
    # 默认关闭：客服问答场景不需要这种深度推理，值得关闭换响应速度。
    # 仅对 OpenAI 兼容 LLM provider 生效（app/providers/openai_compatible.py），
    # 不支持这个参数的供应商预期会直接忽略未知字段，不受影响。
    enable_thinking: bool = False


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_EMBEDDING_", env_file=".env", extra="ignore")

    base_url: str
    api_key: str
    model: str
    dimension: int
    # 部分供应商（如阿里百炼）单次 embeddings 请求最多接受的文本条数有硬
    # 限制，超过直接 400；不设置则一次性发全部文本（兼容没有这类限制的
    # 供应商）。
    batch_size: int | None = None
    # 多个 embeddings 批次间的并发数，默认 1（严格串行，等价于改造前的
    # 逐批 await 行为）。不像 OCR（见 pdf_parser.py 的 ocr_max_concurrency）
    # 已经用真实请求测过账号的并发承受能力，embedding 端点还没有实测过，
    # 默认值保守到"不主动改变现状"，需要时先用小样本实测再调大。
    max_concurrency: int = 1


class MilvusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_MILVUS_", env_file=".env", extra="ignore")

    uri: str = "http://localhost:19530"
    collection: str = "faq_chunks"


class RerankSettings(BaseSettings):
    """为可选项：不配置时 /qa 直接跳过精排，仅走 RRF 融合排序。"""

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_RERANK_", env_file=".env", extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class OcrSettings(BaseSettings):
    """为可选项：三项配置任一缺失则 parse_pdf/parse_image 对无文字层的
    页面/图片直接跳过（不报错，但产出 0 chunk）——常见于扫描件 PDF。
    阿里百炼 compatible-mode 端点（EMBEDDING_BASE_URL 同款）本身就能跑
    qwen-vl-ocr，同一把 embedding_api_key 通常可以直接复用，不必单独申请。
    """

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_OCR_", env_file=".env", extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str = "qwen-vl-ocr"
    # 扫描件 PDF 页面渲染成图片再走 OCR 时的分辨率——72 太低，公式上下标/
    # 数字后缀单位（如"671B"里的"B"）这类细小笔画容易被识别错，实测 200
    # 能显著改善（见 app/ingestion/pdf_parser.py 的说明）。
    render_dpi: int = 200
    # 扫描件 PDF 逐页 OCR 的最大并发数：串行处理一份上百页的文档要
    # 1.5-2 小时以上，OCR 是网络 I/O 为主，值得并发。8 是实测出来的值——
    # 供应商在更高并发下会排队（不报错，只是变慢），不是并发越高越快，
    # 具体数值因账号/供应商而异，见 app/ingestion/pdf_parser.py 里
    # 2026-08-10 的并发排查记录。
    max_concurrency: int = 8


class TableExtractionSettings(BaseSettings):
    """为可选项：不配置模型则 parse_pdf 用 PyMuPDF find_tables()
    + 规则猜表头的老逻辑（见 pdf_parser.py 的 _table_chunks_for_page）；
    配置了模型则改用视觉大模型直接从页面截图语义提取表格数据，比规则
    更泛化——2026-08-10 用真实财报文档验证过，规则处理不好的"多分区
    表格""逐行 key-value 简介表""双栏并排数据"这几类场景，视觉模型都
    能正确处理。复用 OCR 同一个百炼账号（ocr_base_url/ocr_api_key），
    只是模型必须换成支持指令跟随的通用视觉模型——qwen-vl-ocr 是专用
    OCR 模型，会完全无视结构化提取指令；qwen-vl-plus 会漏内容+识别
    错误；只有 qwen-vl-max 实测完整准确，见 table_extraction.py 里
    _DEFAULT_MODEL 的说明。
    """

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_TABLE_EXTRACTION_", env_file=".env", extra="ignore")

    model: str | None = None
    # 2026-08-10 用真实请求对同一账号做过并发梯度实测（4/8/20/40 对比，
    # 每档 20-40 次真实调用，按时间戳逐次核对）：全程 0 个 429/超时，且
    # 同一页在不同并发档位下耗时几乎不变（耗时由该页输出长度决定，不是
    # 排队等待）——不像 qwen-vl-ocr 端点那样存在隐性排队上限（对比见
    # OcrSettings.max_concurrency 的说明）。默认给 40，这是目前测过的最高
    # 并发档位，不代表账号真实上限就是 40（没有再往上测）；换账号/供应商
    # 需要重新用同样的方法（同一批文档、控制变量对比不同并发数）实测。
    max_concurrency: int = 40


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_INGESTION_", env_file=".env", extra="ignore")

    # 摄取任务队列跨文档并发数。默认 1（严格串行，和这次改造前完全一致）
    # ——提高这个值前，除了要用同一份文档批量、控制变量对比不同并发数的
    # 方法（本仓库 OcrSettings.max_concurrency/TableExtractionSettings.
    # max_concurrency 都是这么定下来的）实测多文档同时摄取时账号的真实
    # 承受能力，还必须先解决三个目前只在 job_concurrency=1 时被"顺序执行"
    # 掩盖掉的问题（2026-08-10 最终代码评审发现，详见
    # docs/superpowers/plans/2026-08-10-qa-and-ingestion-concurrency-
    # optimization.md）：
    # 1) 同一个 (tenant_id, file_path) 可能同时有多条 pending 任务（内容
    #    改了两次、或 build_graph 标志不同），并发处理会导致
    #    delete_by_source/ingest 交错执行，两个版本的 chunk 都可能残留
    #    在向量库里——需要按 (tenant_id, file_path) 分组，组内仍然串行；
    # 2) embedding/图谱抽取的并发预算目前不是跨文档共享的（每次
    #    embedding_registry.run() 内部各自新建 Semaphore），job_concurrency
    #    调高会让这两类调用的实际并发数被乘以 job_concurrency，需要参照
    #    ocr_semaphore/table_semaphore 的做法做成跨文档共享；
    # 3) 摄取用到的所有 CPU 密集型同步工作（_prepare_pdf_sync 等）都走
    #    asyncio 默认线程池，和 BM25 检索、Milvus 调用共用同一个池子，
    #    job_concurrency 调高可能让长时间的 PDF 渲染任务饿死这些延迟敏感
    #    的调用，需要单独配一个专用线程池。
    job_concurrency: int = 1

    # 增量摄取的变更追踪+任务队列存储，SQLite 文件路径。
    db_path: str = "data/ingestion.sqlite3"


class Neo4jSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_NEO4J_", env_file=".env", extra="ignore")

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "changeme123"


class NeptuneSettings(BaseSettings):
    """graph_backend="neptune" 时使用，见
    docs/superpowers/specs/2026-08-26-pluggable-graph-backend-design.md。"""

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_NEPTUNE_", env_file=".env", extra="ignore")

    endpoint: str = ""
    port: int = 8182


class AsrSettings(BaseSettings):
    """均为可选项，三项配置任一缺失则语音识别功能不可用。"""

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_ASR_", env_file=".env", extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class TtsSettings(BaseSettings):
    """均为可选项，三项配置任一缺失则对应功能不可用。"""

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_TTS_", env_file=".env", extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    # 阿里百炼 CosyVoice 系列 TTS 专用配置（走 dashscope SDK 的 WebSocket
    # 协议，不是 OpenAI 兼容 REST 接口，见 dashscope_tts.py）。三项都设置
    # 时优先于上面的 base_url 通用 provider；voice 必须是提前用
    # dashscope VoiceEnrollmentService 克隆好的 voice_id，标准音色名在
    # 私有部署端点上不可用（实测报 InvalidParameter）。
    dashscope_websocket_url: str | None = None
    dashscope_http_url: str | None = None
    dashscope_voice: str | None = None


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_AGENT_", env_file=".env", extra="ignore")

    # Agent 自主规划（Planner<->ToolCall 循环）总开关，默认关闭——关闭时用
    # 确定性检索路径，是 Planner 路径出问题时的回退（见
    # docs/AGENT_PLANNER_DESIGN.md）。语音请求无论这里怎么配置都强制走
    # 确定性路径，避免多轮 LLM 往返和首包延迟的硬性要求冲突。
    enable_autonomous_planning: bool = False
    max_tool_call_rounds: int = 3
    # 真实向量库几乎总能返回 Top-K 个最近邻，哪怕语义上完全不相关；设置后，
    # 检索到的记录即使非空，最高相关性分数低于这个阈值也会转人工工单，而不
    # 是把不相关资料硬塞给 LLM。默认不设置（None），行为与之前完全一致——
    # 具体阈值需要结合实际 embedding 模型/语料标定，不能瞎猜一个通用值。
    # 注意：配置了 rerank_provider 时，这里比较的是 rerank 返回的
    # relevance_score（不同供应商的分数范围/语义可能不同，比如有的是 0-1
    # 概率值，有的是无界的原始 logit），不是向量检索阶段的余弦相似度
    # （0-1 有界）——标定这个阈值必须参照实际接入的 rerank 模型的分数
    # 分布，不能沿用向量相似度的经验值。未配置 rerank_provider 时才是
    # 比较向量相似度。
    min_relevance_score: float | None = None


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_MEMORY_", env_file=".env", extra="ignore")

    # 长期记忆召回（app/memory/recall.py::recall_memory_items）默认融合
    # BM25 关键词排名 + embedding 语义排名两路做 RRF。语义这一路每次问答
    # 都要多打一次 embedding API 请求，且是 memory_recall_node 里在拿到
    # 检索结果之前就必须等完的一跳——2026-08-12 排查"响应到第一个字之前
    # 等太久"时，作为临时降级手段加的开关：置 False 时只用 BM25 关键词
    # 排名，跳过这次 embedding 调用。这是纯粹的延迟/召回质量取舍，不是
    # 默认应该关闭的功能，等前面几跳（correction_check、query 改写等）的
    # 延迟优化验证有效后，应该重新评估要不要打开。
    recall_use_embedding: bool = True

    # 对话记忆存储（会话滑窗+长期记忆条目），SQLite 文件路径。
    db_path: str = "data/memory.sqlite3"


class SessionWindowSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_SESSION_WINDOW_", env_file=".env", extra="ignore")

    # 会话滑窗存储后端："sqlite"（默认，复用 memory_conn）或 "redis"
    # （并发扩展性考虑，见 app/memory/session_window_store.py）。类型限定
    # 为 Literal，是为了让拼写错误（比如 "Redis"/"redsi"）在 Settings 构造
    # 时就报错，而不是被 session_window_factory.py 里的字符串精确匹配
    # 悄悄当成"非 redis"、静默退化成 sqlite 默认行为。
    #
    # !!! 生产环境慎用 "redis" !!! 目前只有写入路径（memory_save_node，
    # 见 app/agent/graph.py）迁移到了这个可插拔的 store 抽象；读取路径——
    # app/memory/context_injection.py 的 get_recent_turns（近期会话轮次
    # 注入）和 app/memory/structured_recall.py 的 query_turns_in_window
    # （P1 结构化历史检索）——仍然直接读 SQLite 的 conversation_turns 表，
    # 没有经过这层 store 抽象。这是刻意的分阶段迁移（读路径迁移见后续
    # 任务），但意味着：如果现在就把这个值配成 "redis"，写入会去
    # Redis，而两个读取路径还在查一张永远不会再被写入的 SQLite 表——
    # 近期对话上下文和结构化历史检索会静默返回空结果，没有任何报错
    # 提示。读路径完成迁移之前，生产环境请保持默认的 "sqlite"。
    backend: Literal["sqlite", "redis"] = "sqlite"


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_GATEWAY_", env_file=".env", extra="ignore")

    # 网关注入 tenant_id 时的共享密钥校验（见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md）。
    # 未配置时（本地开发默认）自动降级信任客户端自报的 tenant_id，仅打印
    # 警告日志；生产环境必须配置，否则 tenant_id 可被任意伪造，Milvus/
    # Neo4j 层面即使做了按 tenant_id 过滤的隔离也形同虚设。
    shared_secret: str | None = None


class Settings(BaseSettings):
    """项目全局配置，按领域拆成子模型（见下）——每个子模型自己就是一个
    独立的 BaseSettings，各自带自己的 env_prefix，不是靠 pydantic 的嵌套
    分隔符（env_nested_delimiter）从一个大前缀里切出来的：这样每个子模型
    的字段名去掉领域前缀后（如 llm_base_url -> LLMSettings.base_url）对应
    的环境变量名和改造前完全一致（CUSTOMER_RAG_LLM_BASE_URL 不变），不需要
    同步修改任何 .env/部署配置——见 2026-08-27 架构评审的讨论。

    子模型的分组严格按"字段现有名字的前缀"来，不是按更粗粒度的业务领域：
    Neo4j 和 Neptune 分成两个子模型而不是合并成一个 GraphSettings，是因为
    `neo4j_uri`/`neptune_endpoint` 这些字段名本来就没有共同前缀；ASR 和 TTS
    同理拆成两个。upload_dir/graph_backend/terminology_path/
    graph_review_db_path/redis_url/admin_token/banned_terms 这几个字段
    留在顶层不分组，是因为它们的字段名不共享任何前缀，没有能对齐的分组。
    """

    model_config = SettingsConfigDict(env_prefix="CUSTOMER_RAG_", env_file=".env", extra="ignore")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    table_extraction: TableExtractionSettings = Field(default_factory=TableExtractionSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    neptune: NeptuneSettings = Field(default_factory=NeptuneSettings)
    asr: AsrSettings = Field(default_factory=AsrSettings)
    tts: TtsSettings = Field(default_factory=TtsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    session_window: SessionWindowSettings = Field(default_factory=SessionWindowSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)

    # 后台管理系统上传文件的落盘目录，摄取任务队列按 file_path 读取磁盘
    # 文件（不是直接存字节到数据库），见 app/api/admin_document_routes.py。
    upload_dir: str = "data/uploads"

    # 图数据库后端选择："neo4j"（默认）| "neptune"。见
    # docs/superpowers/specs/2026-08-26-pluggable-graph-backend-design.md。
    graph_backend: Literal["neo4j", "neptune"] = "neo4j"
    # 默认指向占位示例数据，正式环境必须替换为真实术语表文件路径。
    terminology_path: str = "app/graphrag/terminology_seed.yaml"
    # GraphRAG 人工待审核队列存储，SQLite 文件路径。
    graph_review_db_path: str = "data/graph_review_queue.sqlite3"

    redis_url: str | None = None

    # 后台管理系统的管理员 token（登录凭证），未配置时 /api/admin/auth/login
    # 直接拒绝所有登录请求（而不是静默放行）——这和 GatewaySettings.
    # shared_secret 的"未配置=本地兜底"降级路径不同，后台管理能直接写库
    # （上传文档、批准/驳回知识图谱关系），没有"无鉴权也能跑"的必要性。
    admin_token: str | None = None

    # 逗号分隔的自定义敏感词列表，留空 = 不启用自定义敏感词检测（只有
    # check_text() 内置的手机号/身份证号/邮箱正则生效）。解析逻辑见
    # app/api/deps.py::parse_banned_terms。
    banned_terms: str | None = None
