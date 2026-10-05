# Task: 审计直接尝试收尾

停止工具操作，不继续推进或等待，只总结当前 session 已得到的关键观察与条件。只返回 fact，不返回 complete；未完成不等于不存在漏洞，事实不得自报 verified。

仅返回原始 JSON：
{"accepted":true,"data":{"fact":{"description":"关键观察与限制","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"已有证据与缺口"}}}}

拒绝返回 {"accepted":false,"reason":"具体原因"}。保留真实源码相对路径、行号和材料引用，不猜测哈希。

## Origin
{origin}
## Goal
{goal}
## Hints
{hints}
## 固定审计上下文
{audit_context}
