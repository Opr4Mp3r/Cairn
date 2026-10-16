# Task: 审计直接尝试收尾

停止工具操作，不继续推进或等待，只总结当前 session 已得到的关键观察与条件。只返回 fact，不返回 complete；未完成不等于不存在漏洞，事实不得自报 verified。

仅返回原始 JSON：
{"accepted":true,"data":{"fact":{"description":"对象 X 创建后的写入未更新其已有快照；其他创建时序尚未验证。","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"已有证据与缺口"}}}}

Fact description 是可复用的结论卡片，只写条件、能力或阻断及未建立部分，首段保留关键身份、对象、范围和时序；不追加逐行推导、实验步骤或输出复述，review_notes 简述缺口并指向已有材料，不要求新写报告。固定任务契约（fixed_contract）不能被局部任务改写。拒绝返回 {"accepted":false,"reason":"具体原因"}。保留真实源码相对路径、行号和材料引用，源码证据 hash 由程序补齐。

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
