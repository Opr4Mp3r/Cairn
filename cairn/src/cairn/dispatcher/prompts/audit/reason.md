# Task: 审计研究判断

先读取完整 Graph 文件和指定源码。审计上下文固定用户目标、部署基线、攻击者能力、信任边界和必要证据；不能自行放宽条件，不允许以“证明不可能”完成。

所有可引用 Fact IDs 包括待验证对象，用于提出验证 Intent；完成证明只能使用审计上下文 verified_fact_ids 中的 Fact，且逻辑前提都成立。unverified/refuted 主张不能因先前报告自称已确认而成为证明。条件性发现必须保留条件，CVSS 指标不能代替默认部署或漏洞效果。

只返回原始 JSON：
- 拒绝：{"accepted":false,"reason":"具体原因"}
- 有已证实完成证明：{"accepted":true,"data":{"complete":{"from":["f001"],"description":"各项目标及条件的证据"}}}
- 需要探索/验证：{"accepted":true,"data":{"intents":[{"from":["origin"],"description":"明确可验证方向"}]}}
- 有足够正在执行的方向：{"accepted":true,"data":{}}

最多提出 {max_intents} 个互不重叠方向。Open Intents 为空且目标未满足时必须提出方向；不能把重复规划当成进展。只作判断，不执行新的实验、不写源码、不直接调用 Cairn API。Complete 仅提交完成申请，由独立终审批准后才能完成项目。

## Graph
{graph_yaml}
## 可引用 Fact IDs
{fact_ids}
## Open Intents
{open_intents}
## 固定审计上下文
{audit_context}
