# Task: 执行一个代码审计探索

从 Current Intent 和来源 Fact IDs 开始，按入口读清本轮使用的来源结论与 conditions，查看 Hint 导航并按问题展开正文。无需先通读全图；需要互补条件、核对逻辑前提或解决冲突时，再沿导航扩大到其他分支、Kritt 和源码。仅推进 Current Intent；固定任务契约（fixed_contract）是目标与约束的权威来源，局部任务不能预先裁定完整 Goal 已成立。区分存在代码、真实可达、匿名可达和实际危害；追踪路由、框架鉴权和中间件，主动寻找最弱前提的反证。

围绕当前假设深入研究，可跨文件和实际依赖追踪。验证组合时还原“初始状态→前一步→新状态→后一步”，检查传递的具体对象、身份、作用范围、版本和生效时序；分别成立不保证能衔接，后续能力必须说明前序来源。明确成立、仅在条件下成立或被什么阻断，局部排除不扩大为全局不可能。

需要互补材料时，使用审计上下文 kritt_query 中可用的 command，向 stdin 输入 describe/search/get JSON，按当前缺口查询并读取材料和回执。正常功能、低排名及无 finding 流程仍有价值；标签和查询未命中不构成证明。入口不可用或材料不足时按需从固定源码独立发现，不切换 scan。

本次产出是增量观察，不是自动验证或最终完成。所有观察 review_status 必须为 unverified；记录所有必要条件、验证层次、缺口和真实源码证据。条件性机制不能省略配置或身份条件变成无条件发现。premise_ids 只列该主张成立的逻辑前提，不自动复制全部 Intent 来源。无法确认也是有效局部观察，不扩大成全局不存在漏洞。

Intent 的来源用于定位任务和材料，Kritt 记录及先前观察不因此成为逻辑依赖。若本次直接读取的源码足以独立支持观察，premise_ids 为空；只有结论确实依赖另一 Fact 成立时才列它。

description 是供其他分支复用的结论卡片，只写成立条件、已获得的能力或阻断、尚未建立的部分；首段保留决定能否衔接的对象、身份、作用范围与状态／时序。不要在后文接逐行推导、实验步骤或输出复述。conditions 列必要条件，review_notes 简述关键反证、缺口和实际材料入口；详细分析与实验对照按需保存在任务工作目录，通过入口读取，不要求简单观察另写报告。复用条件明确的已有结论，把研究重点放在新连接、关键未证前提和冲突；verified 仍不表示其条件已在当前路径满足。

不修改固定源码；需构建或实验时用其外部的可写副本，材料留在任务工作目录，不直接调用 Cairn API。最终输出仅含一个 JSON 对象，未建立部分和实际材料入口放入 review_notes，不在 JSON 前后添加文字：
{"accepted":true,"data":{"description":"在配置 P、身份 U 下，可以改变对象 X；尚未证明允许的初始攻击者能获得 U。","audit":{"evidence":[{"path":"相对源码路径","start_line":1,"end_line":1}],"conditions":["配置 P","身份 U"],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"身份 U 的取得过程尚未建立；详细依据见实际材料路径。"}}}

引用真实存在的文件和行号，源码证据 sha256 由程序补齐，无需生成或复制。实验依赖的版本／内容一致性确实影响结论时可按需核对，详细记录留在材料中。没有合适源码证据时 evidence 可为空，并说明验证缺口。若仅遇到工具故障、超时或零执行且没有目标项目的新观察，返回 {"accepted":false,"reason":"实际执行问题"}，不写故障 Fact；其余拒绝使用同一格式。不要输出 Markdown 围栏或其他文字。

## Graph
{graph_yaml}
## Current Intent
{intent_id}
{intent_description}
## 来源 Fact IDs
{intent_sources}
## 固定审计上下文
{audit_context}
