# Task: 审计研究判断

先看 Graph 导航、Open Intents 和 Hint，再按当前缺口读取原节点、材料及源码；导航预览不代替完整主张。固定用户目标、部署基线、初始攻击者能力和信任边界，不以“证明不可能”完成。

参考审计上下文 reason_memory 的上轮规划和失败原因，结合当前图重新判断。没有新依据时暂搁已受阻的局部分支，记住重开条件；故障不是源码反证，不能靠改写同一任务反复重试。

围绕值得深入的假设分配探索：新能力能满足哪条路线的缺口，当前缺口能否由其他分支提供，执行前一步后的状态能否支持下一步？核对具体对象、身份、作用范围、部署与生效时序；初始能力固定，后续能力须有前序来源。Intent 说明要查清哪个未知、什么观察会改变判断，可共享来源 Fact。

需要新材料时，使用审计上下文 kritt_query 中可用的 command，向 stdin 输入 describe/search/get JSON，按对象、输入、阻断条件和路径查询并读取返回材料。正常功能、低排名及无 finding 流程都可能有用；查询过宽时缩小问题。Kritt 标签不是证据，未命中不代表不存在；入口不可用或材料不足时可从固定源码独立推导，保留未知，不切换 scan。

可引用待确认观察提出验证 Intent，普通探索无需等待逐条复核。完成证明只能使用 verified_fact_ids 中的 Fact，且所有条件和逻辑前提在同一连续路径中得到支持；verified 仅支持声明的条件命题，不表示条件已在本次部署成立。CVSS 指标不能代替部署或目标效果。

只返回原始 JSON：
- 拒绝：{"accepted":false,"reason":"具体原因"}
- 有已证实完成证明：{"accepted":true,"data":{"complete":{"from":["f001"],"description":"各项目标及条件的证据"}}}
- 需要探索/验证：{"accepted":true,"data":{"intents":[{"from":["origin"],"description":"待验证假设、缺口及预期观察"}],"planning_notes":"已考虑的连接、暂搁原因、重开条件和下一步重点"}}
- 有足够正在执行的方向：{"accepted":true,"data":{}}

规划有变化时，用可选字符串 planning_notes 简述考虑过的连接、暂搁原因和重开条件，无新判断可省略；它是可修订工作记忆，不是 Fact 或证明。最多提出 {max_intents} 个不重复研究同一问题的方向。Open Intents 为空且目标未满足时必须提出具体方向；不能只返回备注或空动作。已有任务足以推进时可以等待。只作判断和定向阅读／查询，不执行新实验、不写源码、不直接调用 Cairn API。Complete 仅提交独立终审申请。

## Graph
{graph_yaml}
## 可引用 Fact IDs
{fact_ids}
## Open Intents
{open_intents}
## 固定审计上下文
{audit_context}
