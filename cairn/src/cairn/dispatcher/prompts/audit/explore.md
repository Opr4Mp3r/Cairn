# Task: 执行一个代码审计探索

根据 Current Intent 及来源 Fact IDs，按需读取相关事实、逻辑前提、Hint 和真实源码；需要时搜索完整 Graph 文件。仅推进 Current Intent，不改变固定 policy、部署基线或初始能力约束。区分存在代码、真实可达、匿名可达和实际危害；追踪路由、框架鉴权和中间件，主动寻找最弱前提的反证。

围绕当前假设深入研究，可跨文件和实际依赖追踪。验证组合时还原“初始状态→前一步→新状态→后一步”，检查传递的具体对象、身份、作用范围、版本和生效时序；分别成立不保证能衔接，后续能力必须说明前序来源。明确成立、仅在条件下成立或被什么阻断，局部排除不扩大为全局不可能。

需要互补材料时，使用审计上下文 kritt_query 中可用的 command，向 stdin 输入 describe/search/get JSON，按当前缺口查询并读取材料和回执。正常功能、低排名及无 finding 流程仍有价值；标签和查询未命中不构成证明。入口不可用或材料不足时按需从固定源码独立发现，不切换 scan。

本次产出是增量观察，不是自动验证或最终完成。所有观察 review_status 必须为 unverified；记录所有必要条件、验证层次、缺口和真实源码证据。条件性机制不能省略配置或身份条件变成无条件发现。premise_ids 只列该主张成立的逻辑前提，不自动复制全部 Intent 来源。无法确认也是有效局部观察，不扩大成全局不存在漏洞。

Intent 的来源用于定位任务和材料，Kritt 记录及先前观察不因此成为逻辑依赖。若本次直接读取的源码足以独立支持观察，premise_ids 为空；只有结论确实依赖另一 Fact 成立时才列它。

description 首段给出可独立引用的主要结论及关键条件，后文保留细节、阻断和来源。对象归属、作用范围、状态与时序差异不能为缩短结论而省略；摘要不足时回原文确认。verified 的来源 Fact 仍须核对其条件能否在当前路径满足。

不修改固定源码；需构建或实验时用其外部的可写副本，材料留在任务工作目录，不直接调用 Cairn API。最终输出仅含一个 JSON 对象，进度说明或版本疑问放入 review_notes，不在 JSON 前后添加文字：
{"accepted":true,"data":{"description":"本次已观察内容及条件、缺口","audit":{"evidence":[{"path":"相对源码路径","start_line":1,"end_line":1}],"conditions":["必要条件"],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"支持、反证或未确认内容"}}}

引用真实存在的文件和行号，不猜测 sha256，哈希由 Dispatcher 计算。没有合适源码证据时 evidence 可为空，并说明验证缺口。若仅遇到工具故障、超时或零执行且没有目标项目的新观察，返回 {"accepted":false,"reason":"实际执行问题"}，不写故障 Fact；其余拒绝使用同一格式。不要输出 Markdown 围栏或其他文字。

## Graph
{graph_yaml}
## Current Intent
{intent_id}
{intent_description}
## 来源 Fact IDs
{intent_sources}
## 固定审计上下文
{audit_context}
