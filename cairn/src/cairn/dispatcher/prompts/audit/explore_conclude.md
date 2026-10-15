# Task: 收尾当前审计探索

停止进一步研究、工具操作和等待，只总结当前 session 已获得的增量观察、必要条件、原始材料路径和未确认项。不能把执行超时当作反证，不能提升事实状态或宣称 Goal 完成。

只使用当前 session 已有上下文，不读取新文件、不重新做实验。仅返回原始 JSON：
{"accepted":true,"data":{"description":"当前可保留的观察和限制","audit":{"evidence":[],"conditions":[],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"实际证据及缺口"}}}

description 首段保留主要结论和关键条件，后文说明对象、身份、状态／时序差异及未闭合连接。premise_ids 只列逻辑依赖，不复制全部任务来源；分别成立的观察不能概括成已验证的组合。

evidence 可引用 session 已读取的真实源码相对路径与行号；不要猜测内容哈希。仅有故障或零执行、没有目标项目的新观察时返回 {"accepted":false,"reason":"实际执行问题"}，不把故障写成 Fact；其余拒绝使用同一格式。不要输出新 Intent 或 complete。

## Current Intent
{intent_id}
{intent_description}
## 固定审计上下文
{audit_context}
