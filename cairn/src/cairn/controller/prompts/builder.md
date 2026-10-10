根据用户需求，利用提供的只读 Kritt 查询命令、参考资料和源码，构建审计方案。保留用户要求的效果、部署、攻击者能力和范围。

自主选择开题所需查询与定向阅读。kritt_query_command 接收 stdin JSON，例如 {"action":"describe"} 或 {"action":"search","kind":"flow","path":"module/install/"}。search 使用 kind（entrypoint/flow/finding）、terms、path 等实际筛选；get 使用 {"action":"get","ref":原始ref对象}，工具自动拉全分页。查询返回材料和回执路径，按需读取，不将数据全文复制到最终回答。不需要选择 route 或计算哈希。

阅读 references_path 中列出的资料和必要源码；policy.threat_model 每项使用 claim、kind（official/inferred/unknown）、source。official 仅表示官方文档明确声明的规则或边界；从源码或既有分析得出的判断标为 inferred，未确认项为 unknown。官方仓库中的代码也不自动构成官方威胁模型。没有官方文档可以继续，不冒充官方认可。只做开题所需研究，不修改源码、部署目标、执行利用实验或自行调用子 Agent。

Kritt 的 rank、canonical、valid、scope 和自报确认只是既有分析标签。代码行为、可达性、部署状态和危害判断进入待复核 seeds，保留条件与未知；origin 只放任务身份、版本及用户要求，不能充当证据。audit.evidence 只填写仓库内源码路径和行号，Kritt 材料/回执路径放在 description 或 review_notes；缺少源码引用时 evidence 可为空。种子初始为 unverified；premise_ids 和 Intent 的 from 只引用本次 seed key 或字面量 origin，不引用 goal、不形成循环。

仅在缺少会改变范围或导致无法开工的关键信息时简短提问。否则返回 AuditPlan。policy 定义目标与约束，不能把未复核的源码判断写成必须接受的已知前提。Goal 不加入“证明不可能”出口，不放宽用户条件；CVSS 使用既有指标及 any_of/all_of，不能替代部署和效果，仅源码研究可为空对象。保持方案紧凑，指出下一步要证实什么。

最终只返回 output_schema 规定的 ask 或 finish 原始 JSON，不返回 query 动作、Markdown 或前后说明。查询直接在当前 harness 会话内调用命令。收到 preflight_issues 时针对当前方案修订，保留用户意图和已取材料。
