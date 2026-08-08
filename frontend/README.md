# 客服智能问答 Demo（前端）

基于 Agent 推理核心 + GraphRAG + 检索增强的企业客服问答系统体验 Demo。视觉设计详见
[`DESIGN.md`](./DESIGN.md)（neo-brutalism 风格，实测自 raft.build/zh-cn，米白背景+硬边框+
硬投影，不是深色主题）。

## 启动前置条件

1. **后端环境配置**：仓库根目录的 `.env` 需要配好真实的 `CUSTOMER_RAG_EMBEDDING_*`、`CUSTOMER_RAG_LLM_*`（摄取和问答都要真实调用这些 API），以及可用的 Milvus（`CUSTOMER_RAG_MILVUS_URI`）。如果要体验 GraphRAG 术语强制注入能力，还需要配置好 Neo4j（`CUSTOMER_RAG_NEO4J_*`）。
2. **摄取示例语料**（必须先做这一步，否则打开 demo 问什么都会得到"未找到确切答案，已转人工"的兜底话术）：

   ```bash
   .venv/Scripts/python.exe -m app.ingestion.main --dir docs/demo-data --tenant-id demo --build-graph
   ```

   `--tenant-id demo` 必须和这个前端硬编码的租户一致（见下方"已知限制"）。
3. **启动后端**（必须监听 8000 端口，`vite.config.ts` 的开发代理硬编码了这个端口）：

   ```bash
   .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
   ```

## 启动前端

```bash
cd frontend
npm install   # 首次运行需要
npm run dev
```

打开浏览器访问 `http://localhost:5173`（**必须用 `localhost`，不要用局域网 IP 访问**——聊天界面用了 `crypto.randomUUID()` 生成会话 ID，这个 API 只在浏览器认定的安全上下文里可用，`http://localhost` 是安全上下文，但 `http://192.168.x.x` 这类局域网 IP 不是，会导致页面加载就报错白屏）。

## 已知限制

- 租户固定写死为 `"demo"`（`frontend/src/hooks/useAgentChat.ts` 里的 `TENANT_ID` 常量），没有租户切换功能——摄取语料时也必须用同一个 `--tenant-id demo`，否则前端检索不到任何内容。
- 只支持文字问答，不支持语音输入/输出。
- 只有浅色主题（米白+黑边框），没有深色模式切换。
- 不引入自动化测试框架，`npm run typecheck`/`npm run build` 是主要的正确性验证手段。

## 人工验收清单

功能实现完成后，建议在浏览器里逐项确认：

1. 打开 `http://localhost:5173`，Hero 区标题/副标题正常显示，米白背景+黑色硬边框+糖果色
   强调色符合预期（黄色导航栏、粉色主按钮）。
2. 输入"网关超时示例是什么意思？"，观察回答是否有逐句流式出现的效果（不是一次性蹦出全部文字）。
3. 回答下方出现来源引用标签，格式类似"📄 docs\demo-data\faq-error-e502.md#0"（含目录前缀、chunk 序号，这是正常格式，不是精简过的纯文件名）。
4. 连续追问一个指代不明的问题（如先问"E502 怎么处理"，再问"那这个问题会不会丢数据"），观察多轮对话是否连贯（依赖后端记忆/指代消解能力）。
5. 关掉后端进程后再发一条消息，确认界面能看到"连接后端失败"的错误提示（追加显示，不会抹掉已经流式显示出来的部分回答），而不是卡死无响应。
6. 用浏览器开发者工具的移动端模拟视图，确认页面在窄屏下没有明显的布局错乱（没有专门做响应式适配，这里只是留意有没有严重问题）。
