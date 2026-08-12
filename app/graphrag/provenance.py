from __future__ import annotations

# Neo4jGraphClient.merge_relation() 的 provenance 参数只允许这两个取值，
# 集中定义在这里供所有写入路径共用（app/graphrag/normalization.py 的自动
# 写入路径、app/graphrag/review_queue.py 的人工批准路径），避免字符串
# 字面量在多处各写一份、容易打错或不同步。
#
# 这是 2026-08-12 补的可观测性字段：在此之前，一条关系边一旦写入 Neo4j，
# 就无法区分它是摄取时术语表精确对齐后自动写入的，还是未对齐候选经人工
# 审核批准后写入的——两条路径调用的是同一个 merge_relation，边上只有
# source/tenant_id 两个属性，检索侧（term_guard/graph_query_tool）也确实
# 不区分来源、一视同仁地返回。加这两个值不改变检索行为（仍然不做来源
# 过滤），只是让"这条边有没有被人看过"这件事变得可查——为后续的质量
# 审计/问题排查提供依据。
AUTO_MERGED = "auto_merged"
HUMAN_APPROVED = "human_approved"
