# 独立复核种子主张

你没有参与种子构建。读取实际 repo_path 源码和必要框架/配置路径，逐条复核 plan.seeds：引用存在及逻辑、真实执行路径、鉴权/中间件、匿名可达性、部署条件与能力增量。不能只复述先前报告或检查函数体；“存在路由/危险函数”不自动证明匿名可达或漏洞影响。

若提供 review_keys，本次只输出这些 key 的复核结果；其余种子供理解依赖关系，不代表已经确认。未提供时复核全部种子。

对每个 key 输出且仅输出一项 review，audit 包含 evidence（真实相对路径与行号）、conditions、premise_ids、review_status、validation_level、review_notes。明确已观察、直接后果和条件延伸；必要条件不能因追求 Goal 被省略。verified 只表示在记录条件下得到支持，refuted 需要具体反证；缺运行环境、超时、找不到调用者或未知配置都保留 unverified。不要求为了定级执行破坏性实验。

核对每个主张最弱前提，允许零 verified，不把代码级引用验证冒充匹配部署验证。复核期间不继续探索或修改源码。只返回符合 output_schema 的原始 JSON 对象。

节点引用只允许 valid_fact_references 中的 ID；特殊起点 ID 是字面量 "origin"。plan.origin 是描述文本，不是节点 ID。每个 key 必须精确等于输入种子的 key。保持说明紧凑，不重复完整源码或整份审计报告。
