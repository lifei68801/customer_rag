# TermGuard 模糊匹配设计方案

> 状态：设计定稿（经用户确认）
> 背景：架构覆盖度审计发现 TermGuard（`app/graphrag/term_guard.py` + `term_matcher.py`）只做精确子串匹配，架构设计文档提到的模糊匹配层从未实现——`term_matcher.py` 自己的 docstring 也承认"不涉及向量相似度/编辑距离的模糊匹配（那属于更进阶的功能，需先用精确匹配跑出数据再评估是否值得加，避免引入误召回风险）"。这是拆分出的 4 个独立子项目（检索层修正、TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配）里的第 2 个。

## 1. 现状与复用点

`match_terms(text, terms)`（`app/graphrag/term_matcher.py`）：遍历每个术语的标准名+别名，原样出现在文本里即命中，返回命中的 `Term` 列表。已核实这个函数只被 `app/graphrag/term_guard.py::build_term_guard_context` 调用，没有其他调用方——改动范围可以完全收敛在 `term_matcher.py` 一个文件，`term_guard.py` 不需要改动。

`app/voice/asr_term_correction.py::_find_fuzzy_candidates` 已有滑动窗口 + `difflib.SequenceMatcher` 模糊匹配的先例：窗口长度等于候选词长度，逐位置滑动计算相似度比值，超过阈值即算候选。这个函数的 `fuzzy_threshold` 参数从未被调用方（`app/api/voice_routes.py`）覆盖，说明本仓库的既定做法是"函数参数给合理默认值，不为这类阈值单独开 Settings 字段"——本次设计沿用同样的约定，不新增配置项。

## 2. 设计

### 2.1 两层匹配

`match_terms(text, terms, *, fuzzy_threshold: float = 0.75)` 扩展为两层：

1. **精确匹配层（不变）**：标准名/别名原样出现在文本里即命中，行为和现在完全一致。
2. **模糊匹配层（新增，仅对第 1 层未命中的术语跑）**：对每个未被精确命中的术语，其标准名+每个别名各自用滑动窗口扫描文本（窗口长度=候选词长度），`difflib.SequenceMatcher(None, span, candidate).ratio() >= fuzzy_threshold` 即算命中该术语。算法直接复用 `_find_fuzzy_candidates` 的核心逻辑，但返回值收敛成 `match_terms` 已有的 `list[Term]` 契约——不需要 ASR 场景里"是否要替换文本"那层区分，模糊命中就和精确命中一样直接算命中。

已跳过精确命中术语的别名/标准名（`if not alias or alias in text: continue`，复用 ASR 校正同款判断），避免对已经命中的术语做无意义的重复扫描。

### 2.2 不引入 LLM 确认（已与用户确认）

模糊命中直接和精确命中一样，走 TermGuard 既有的强制注入图谱上下文流程，不像 ASR 校正那样额外过一道 LLM 判断"要不要真的替换"。

**理由**：
- TermGuard 误命中的代价很轻——只是给系统提示词多塞一段可能不相关的图谱上下文，不会像 ASR 校正那样直接改写用户输入的文本内容，风险等级不对等。
- 保持"TermGuard 是纯确定性安全网，不依赖 LLM 自主判断是否需要查图谱"这条架构设计文档明确强调的核心原则——这正是 TermGuard 存在的意义（LLM 自主决策可能漏调图谱工具，TermGuard 是不依赖 LLM 判断的补丁）。
- 不引入额外的每轮 LLM 调用，不增加延迟和成本。

### 2.3 阈值

`fuzzy_threshold` 默认值定为 **0.75**（比 ASR 校正的 0.6 更保守），因为 TermGuard 这条路径没有 LLM 二次确认兜底误召回，需要更高的相似度门槛才能安全直接触发强制注入。这个数值作为参考起点写在代码注释里，明确标注"需要结合真实数据调整，不是权威值"，不假装一个编出来的数字是权威标定结果。

### 2.4 性能考量

模糊匹配层只对精确匹配层没命中的术语才跑，`term_guard_node` 本来就在每轮对话上跑一次精确匹配，这次是在"精确没命中"的术语上追加一层更贵的滑动窗口扫描——不改变量级，但常数因子会变大（滑动窗口逐位置计算 `difflib.ratio` 明显比子串包含判断贵）。术语表规模较大时这层开销值得关注，但暂不做进一步优化（比如限制模糊层只扫描前 N 个高频术语），YAGNI——真实使用中发现是瓶颈再处理，这次不过度设计。

## 3. 测试

- 新增测试验证模糊匹配确实能命中"标准名/别名的近似变体"（如故意打错一两个字的场景），复用 `tests/graphrag/test_term_matcher.py` 现有的测试术语表。
- 新增测试验证阈值以下的相似文本不会被误命中，防止阈值设太松导致噪声注入。
- 新增测试验证已经被精确匹配命中的术语不会重复走模糊层。
- 现有 3 个精确匹配测试（`test_match_terms_finds_standard_name_via_alias`、`test_match_terms_returns_empty_when_no_alias_or_name_present`、`test_match_terms_dedupes_when_multiple_aliases_of_same_term_present`）保持不变、必须继续通过。

## 4. 范围之外（不做）

- 不引入 LLM 确认层（已确认的设计决策，见 2.2）。
- 不新增 Settings 配置项，阈值是函数参数的硬编码默认值（沿用 ASR 校正的既定约定）。
- 不改动 `term_guard.py`（`match_terms` 的外部契约不变，调用方无感知）。
- 不做性能优化（如限制扫描术语数量），YAGNI。
- 不改动 GraphRAG 实体链接（`normalization.py::resolve_to_standard_name`）的模糊匹配——那是拆分出的第 4 个独立子项目，不在本次范围。
