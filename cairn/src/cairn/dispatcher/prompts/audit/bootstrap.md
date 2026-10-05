# Task: 审计初始直接尝试

审计项目默认不启用此阶段。若被派发，读取固定源码，保留目标、威胁边界和部署条件，只在有完整证明时返回 fact 和 complete。事实仍从 unverified 开始，不能绕过独立终审或伪装条件；没有完整结果则由后续收尾总结。

仅返回原始 JSON：
{"accepted":true,"data":{"fact":{"description":"关键观察及条件","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"支持和缺口"}},"complete":{"description":"目标证明"}}}

拒绝返回 {"accepted":false,"reason":"具体原因"}。不直接写 Cairn API，不改目标源码。

## Origin
{origin}
## Goal
{goal}
## Hints
{hints}
## 固定审计上下文
{audit_context}
