# Task: 执行一个代码审计探索

根据 Current Intent 及来源 Fact IDs，按需读取相关事实、逻辑前提、Hint 和真实源码；需要时搜索完整 Graph 文件。仅推进 Current Intent，不改变固定 policy、部署基线或攻击者能力。区分存在代码、真实可达、匿名可达和实际危害；追踪路由、框架鉴权和中间件，主动寻找最弱前提的反证。

本次产出是增量观察，不是自动验证或最终完成。所有观察 review_status 必须为 unverified；记录所有必要条件、验证层次、缺口和真实源码证据。条件性机制不能省略配置或身份条件变成无条件发现。premise_ids 只列该主张成立的逻辑前提，不自动复制全部 Intent 来源。无法确认也是有效局部观察，不扩大成全局不存在漏洞。

Intent 的来源用于定位任务和材料，Kritt 记录及先前观察不因此成为逻辑依赖。若本次直接读取的源码足以独立支持观察，premise_ids 为空；只有结论确实依赖另一 Fact 成立时才列它。

不修改目标源码；实验材料留在当前工作目录，不直接调用 Cairn API。仅返回一个 JSON 对象：
{"accepted":true,"data":{"description":"本次已观察内容及条件、缺口","audit":{"evidence":[{"path":"相对源码路径","start_line":1,"end_line":1}],"conditions":["必要条件"],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"支持、反证或未确认内容"}}}

引用真实存在的文件和行号，不猜测 sha256，哈希由 Dispatcher 计算。没有合适源码证据时 evidence 可为空，并说明验证缺口。拒绝时返回 {"accepted":false,"reason":"具体原因"}。不要输出 Markdown 围栏或其他文字。

## Graph
{graph_yaml}
## Current Intent
{intent_id}
{intent_description}
## 来源 Fact IDs
{intent_sources}
## 固定审计上下文
{audit_context}
