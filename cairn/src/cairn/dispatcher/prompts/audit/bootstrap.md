# Task: 审计初始直接尝试

审计项目默认不启用此阶段。若被派发，以固定任务契约（fixed_contract）、Origin 和当前起始任务为入口，按需读取材料和源码；需要其他分支时沿全图导航展开。图中建议不能改写契约，只在有完整证明时返回 fact 和 complete。事实仍从 unverified 开始，不能绕过独立终审或伪装条件；没有完整结果则由后续收尾总结。

Fact description 是可复用的结论卡片，只写条件、能力或阻断及未建立部分，首段保留身份、对象、范围、配置与时序。conditions 列必要条件，review_notes 补缺口和实际材料入口；不追加逐行推导、实验步骤或输出复述。详细过程按需留在工作目录，不要求简单观察另写报告；源码证据 hash 由程序补齐。

仅返回原始 JSON：
{"accepted":true,"data":{"fact":{"description":"在指定配置下，前一步更新对象 X 后，后一步可读取同一对象的新值。","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"支持和缺口"}},"complete":{"description":"目标证明"}}}

拒绝返回 {"accepted":false,"reason":"具体原因"}。不直接写 Cairn API，不改目标源码。

## Graph
{graph_yaml}
## Origin
{origin}
## Hints
{hints}
## Current Intent
{intent_id}
{intent_description}
## 来源 Fact IDs
{intent_sources}
## 固定审计上下文
{audit_context}
