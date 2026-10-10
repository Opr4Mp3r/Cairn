# Task: 收尾当前审计探索

停止进一步研究、工具操作和等待，只总结当前 session 已获得的增量观察、必要条件、原始材料路径和未确认项。不能把执行超时当作反证，不能提升事实状态或宣称 Goal 完成。

只使用当前 session 已有上下文，不读取新文件、不重新做实验。仅返回原始 JSON：
{"accepted":true,"data":{"description":"当前可保留的观察和限制","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"实际证据及缺口"}}}

evidence 可引用 session 已读取的真实源码相对路径与行号；不要猜测内容哈希。拒绝时返回 {"accepted":false,"reason":"具体原因"}。不要输出新 Intent 或 complete。

## Current Intent
{intent_id}
{intent_description}
## 固定审计上下文
{audit_context}
