# Task: 收尾当前审计探索

停止进一步研究、工具操作和等待，只总结当前 session 已获得的增量观察、必要条件、原始材料路径和未确认项。不能把执行超时当作反证，不能提升事实状态或宣称 Goal 完成。

只使用当前 session 已有上下文，不读取新文件、不重新做实验。仅返回原始 JSON：
{"accepted":true,"data":{"description":"在配置 P、身份 U 下，可以改变对象 X；尚未证明允许的初始攻击者能获得 U。","audit":{"evidence":[],"conditions":["配置 P","身份 U"],"premise_ids":[],"review_status":"unverified","validation_level":"source_analysis","review_notes":"身份 U 的取得过程尚未建立；详细依据见 session 中已有材料路径。"}}}

description 是可复用的结论卡片，只写成立条件、能力或阻断及未建立部分；首段保留对象、身份、范围、状态／时序。不要追加逐行推导、实验步骤或输出复述。conditions 列必要条件，review_notes 简述缺口和已有材料入口，不要求新写报告。premise_ids 只列逻辑依赖，不复制任务来源。局部效果和分别成立的观察不能概括成完整 Goal 已达成；固定任务契约（fixed_contract）不受当前 Intent 或旧结论改写。

evidence 可引用 session 已读取的真实源码相对路径与行号；源码证据 hash 由程序补齐。仅有故障或零执行、没有目标项目的新观察时返回 {"accepted":false,"reason":"实际执行问题"}，不把故障写成 Fact；其余拒绝使用同一格式。不要输出新 Intent 或 complete。

## Current Intent
{intent_id}
{intent_description}
## 来源 Fact IDs
{intent_sources}
## 固定审计上下文
{audit_context}
