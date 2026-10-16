# 附录：真实任务节点的目标写法

对应[审计上下文最小改造方案](audit-context-minimal-refactor.md)。本附录使用 `8002/proj_001` 已归档的真实记录，展示同一研究内容在优化后应如何交付。

以下是人工编辑的文档样例：真实 ID 用于回查，状态、来源边、证据位置沿用记录；文字收敛并纠正了部分过强概括。它们不是新研究结果、可提交请求或对历史数据的迁移。未来程序只做视图投影，不会自动完成这里的语义改写。新节点由更新后的角色提示词引导生成。

2026-10-16 旁路职责调整覆盖本附录原有“Fact 产出后由 Overseer 复核”的输入示例：新版只观察未结 Intent 并发 Hint，Fact 作为线索按需读取。以下历史节点、h009 的字节码核对叙述及已归档审核状态保留其历史身份；不表示新版旁路会重复执行这些核对或认证 Fact。原运行后来出现的 r093 等审核也不是新版终审结果。

## 1. 样本与时间边界

数据取自 [2026-10-15 16:27:43 +08:00 快照](/Users/opr4/Desktop/cairn+kritt/.worktrees/cairn-compositional-part4-side/.claude/verification/live-review-8002-20261015-162743/snapshot.json)。源码未重新核查，实验未重跑。文中的证据位置是原记录引用；保留 `unverified` 不因写得更清楚而升级。

| 示例 | 真实来源／产出 | 本附录展示的意义 |
| --- | --- | --- |
| `origin`、`goal` | 原项目起点与目标 | 固定契约独立注入，图只保留必要来源和目标锚点 |
| `i037 → f025` | 来源 `[f015, f016]` | 待检验的连接，得到有范围的阻断结论 |
| `i041 → f029` | 来源 `[f001, f004, f021, f022]` | 后段效果得到观察，但允许起点未独立闭合 |
| `h009` | `overseer`，08:19:02Z | 针对分享准入口径的局部纠偏 |
| `planning_notes` | 08:14:54Z 开始的 Reason 记录 | 使用已有阻断调整选题，保留重开条件 |

时间不能混用：`i037` 创建于 07:48:27Z、产出 `f025` 于 08:04:33Z；`i041` 创建于 08:14:51Z、产出 `f029` 于 08:23:11Z。08:14 的规划示例不会引用稍后的 `h009`、`f029`；Intent 示例也不会提前知道其产出。

以下材料相对路径均以研究工作根目录为基准：

```text
/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/
```

## 2. 固定契约：单独注入，不复制进各节点

下面是该任务原 Goal 与 policy 的原样内容，仅调整 JSON 排版。它在每次调用的“固定任务契约”块中出现一次；原始目标的表达缺陷不能靠投影静默改掉。新节点的主张必须在这个契约下解释。

<details>
<summary>展开真实 Goal 与 policy：这是调用输入，不是每个节点的正文</summary>

```json
{
  "goal": "在 JeecgBoot 3.9.5（commit 87d7f938d47d2585618bcfc5a31d125801cbff27）默认单体部署、默认 profile、不修改源码与默认配置（含仓库提交的 signatureSecret、Shiro 白名单原样）的前提下，找到并验证无需任何有效登录凭据即可远程触发的代码/命令执行路径，或等效持久控制（模板注入执行、数据库层命令执行、可执行文件写入+可达包含/加载）。攻击者为匿名网络客户端，可获取匿名页面/响应中泄漏的任何票证或密钥材料。要求：入口匿名可达须落到具体守卫行（Shiro anon 链、@IgnoreAuth、@JimuNoLoginRequired、签名门的可匿名满足性）；仅存在条件链时（需特定数据库类型/FILE 权限、需已发布分享链接、需非默认开关如 enableMinerU）逐项标注条件与默认部署下的满足程度；不放宽到低权限已登录用户；已认证前提的执行面（如 /sys/dict 系列 SQLi、数据源导入、模板导入、MCP 保存与同步、runAiragFlow）仅可作为认证旁路成立后的二级链研究。",
  "policy": {
    "attacker_capabilities": [
      "匿名网络客户端，可达目标 HTTP 端口（含经网关 9999 的 cloud 形态作为条件分支）",
      "可请求任何匿名可达页面/端点并取得其响应中泄漏的票证、cookie、HTML 内嵌密钥材料",
      "可构造任意 HTTP 头（含伪造 unsigned X-Access-Token）与请求体",
      "无任何有效登录凭据、无初始内部网络位置（SSRF 取得的内网位置仅可作为链后续步骤）、无物理访问",
      "诱导内部用户访问恶意页面（仅用于 CORS/postMessage 类前置原语，不作为主路径）"
    ],
    "cvss": {},
    "deployment": "官方仓库默认单体部署：jeecg-module-system/jeecg-system-start 启动，默认 profile（dev 类配置的数据源 MySQL 127.0.0.1:3306/jeecg-boot，uploadType=local，jeecg.path.upload=/opt/upFiles），context /jeecg-boot、端口 8080；源码与默认配置不被修改（signatureSecret 保持仓库提交值，Shiro anon 白名单保持默认）。其他 shipped profile（postgresql/oracle/sqlserver/dm8/kingbase8、docker、cloud gateway 9999 / jeecg-server-cloud 形态）作为条件分支记录，不作为主前提。",
    "effect": "未授权（匿名）远程代码执行：任意命令/代码执行，或等效持久控制（FreeMarker/OGNL/SpEL 等模板注入执行、数据库层命令执行如 xp_cmdshell/COPY PROGRAM/into outfile 落地可执行文件且可被 Web 加载）。纯 SSRF、信息泄露、仅 SQLi 读取不构成本任务目标效果，但可作为链的中间原语研究。",
    "evidence_requirements": [
      "每个结论须给出仓库内源码路径与行号证据；依赖 jar 内行为以 pom 版本锚定，并在本地依赖副本上复核后方可作为已验证事实",
      "匿名可达性结论必须引用 ShiroConfig/JwtFilter/@IgnoreAuth/@JimuNoLoginRequired 的具体行；签名门可满足性须给出可复现的密钥/票证获取路径",
      "Kritt 材料与 receipts 路径写入 description 或 review_notes，不写入 audit.evidence",
      "rank/canonical/valid/scope 等 Kritt 标签只是分析标签，不得当作已确认事实引用"
    ],
    "scope": [
      "jeecg-boot 后端全部生产代码（base-core、module-system、boot-module/airag、server-cloud、aiflow 依赖 jar 的运行时行为）",
      "jeecgboot-vue3 前端生产代码（含提交的签名工具与配置）",
      "仓库 pom.xml 固定版本的 jimureport-spring-boot4-starter 2.5.4、jimubi-spring-boot4-starter 2.5.4、jimuchatbi-spring-boot4-starter 1.2.0、jeecg-aiflow-boot4 3.9.5 的运行时行为（经本地依赖 jar/反编译副本复核）",
      "排除测试代码、构建产物、未修改的其余第三方依赖",
      "验证仅在本地隔离副本进行，不访问公开部署实例"
    ],
    "threat_model": [
      {
        "claim": "JeecgBoot 官方将 /jeecg-boot 后端视为需经反向代理/防火墙保护的内部管理系统，未把匿名 HTTP 面视为必须抵抗远程匿名攻击者的边界",
        "kind": "unknown",
        "source": "仓库 README/部署文档为部署指南，未见表述威胁边界的威胁模型文档；此为待核实的部署假设而非已确认前提"
      },
      {
        "claim": "/jmreport/**、/drag/**、/jimu/chat2bi/**、大屏与分享页白名单是官方有意的匿名功能面（报表/大屏分享给未登录访客）",
        "kind": "inferred",
        "source": "ShiroConfig.java:143-146 注释（积木报表排除/Chat2BI 分享页）"
      },
      {
        "claim": "signatureSecret 前后端一致且『正式发布请自行修改』，官方预期运维在发布时修改该默认密钥",
        "kind": "official",
        "source": "jeecgboot-vue3/src/utils/encryption/signMd5Utils.js:2 注释及 :3 默认值；application-dev.yml:278/:337 同值提交"
      },
      {
        "claim": "airag 聊天分享链路设计为匿名访客可用（@IgnoreAuth + shareToken 门），其身份判定与文件引用限制构成安全边界",
        "kind": "inferred",
        "source": "AiragChatController.java:65 @IgnoreAuth 与 AiragChatServiceImpl.java:2002 checkAnonymousShareAccess 的存在（针对 issues/9672 的匿名加固为 Kritt finding 542/543 的叙述，待复核）"
      }
    ]
  }
}
```

</details>

注意：该 policy 的 `cvss` 原值是 `{}`；威胁模型中分别有 `unknown`、`inferred`、`official`，不能补造评分阈值或把未知边界写成官方结论。涉及不同部署或额外身份的条件性发现仍可保留，但不能据此改变主目标。

## 3. origin 与 goal：来源说明和目标锚点

```yaml
id: origin
description: |-
  本次研究使用 JeecgBoot 3.9.5 的 Kritt scan 1，固定源码提交为
  87d7f938d47d2585618bcfc5a31d125801cbff27。
  上一次暂停任务的结论和实验材料仅作待复核线索，不继承其验证状态。

  源码与固定 Kritt 查询入口见本次调用的材料入口；
  开题选材和研究期查询各自保留回执，详细清单不在此重复。
```

这里保留版本和旧结果的知识地位；检索词列表、全部材料 ID、固定视图 hash 和机器路径清单从模型节点正文移到既有材料入口。原始 origin 完整文本继续保存。

```yaml
id: goal
description: >-
  本次目标与完整约束见本次调用的固定任务契约。
  本节点仅作图中的目标锚点，不定义另一套成功条件。
```

只有第 2 节已在调用中完整展开时，才能采用这个 goal 视图。不能把这两行单独交给 Worker 后声称已经交付目标。

## 4. i037：只提出连接问题，不提前写入答案

```yaml
id: i037
from: [f015, f016]
description: |-
  检查 jimureport 2.5.4 中已有文件或配置写入能力，
  能否通过实际消费者进入加载或执行路径。
  本方向与模板渲染调查分开，重点是写入对象与消费对象能否对应。

  来源节点提供写入和准入线索；相关能力仍须核对其条件。
  若找到消费者，查清对象、身份和生效时序能否连续衔接；
  若连接受阻，记录具体阻断及重新研究所需的新证据。
```

它没有预先加入 f025 后来发现的脚本校验机制。没有复制前序代码分析，也没有重复 JSON、hash 或全局安全约束。来源仍是实际 `[f015, f016]`，不是结论的逻辑前提数组。

## 5. f025：保留有范围的阻断知识

理想首段可以直接读成：**在所查版本和本地配置下，已有受限写入尚不能接到所查执行面；项目其他路径仍未被排除。** 详细机制只保留影响下一轮连接判断的部分。

<details open>
<summary>f025 的完整节点示例：全部原源码定位保留，仅去掉证据 hash</summary>

```yaml
id: f025
description: |-
  在本次分析的 jimureport 2.5.4 与本地单体配置范围内，
  已有受限文件写入尚不能连接到所查加载或执行路径。
  已检查的上传目录消费者仅作读取或嵌入；该结论不排除项目其他路径。

  已找到的 Python 执行组件使用 classpath 脚本并校验名称与内容，
  本次依赖范围内未找到可进入该执行分支的 HTTP 调用方。
audit:
  conditions:
  - 范围限于所查单体默认 profile、local 上传和仓库原有白名单／签名配置；不外推到其他版本或部署。
  - 所查依赖为 jimureport 2.5.4，本地实例内该依赖与分析副本字节一致；外层运行构建与目标源码提交的对应关系未独立核验。
  - 原记录的写入观察依赖有效分享记录和当期／前一小时票证；跨小时未复测，也不证明任意匿名请求均可写入。
  - 写入还受图片可解码、文件名白名单、目标文件不存在及 apiDsIpCheck 未开启的限制。
  - Python 执行组件要求脚本名称命中白名单、磁盘内容与 classpath 资源一致；已有写入不能直接满足这组条件。
  - 所查 HTTP 导出入口另需非空签名配置和 enable-http-export=true，默认条件未满足；不能把入口名等同于执行可达。
  evidence:
  - end_line: 507
    path: jeecg-boot/pom.xml
    start_line: 503
  - end_line: 61
    path: jeecg-boot/pom.xml
    start_line: 60
  - end_line: 83
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/WebMvcConfiguration.java
    start_line: 76
  - end_line: 121
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/ShiroConfig.java
    start_line: 121
  - end_line: 147
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/ShiroConfig.java
    start_line: 146
  - end_line: 213
    path: jeecg-boot/jeecg-module-system/jeecg-system-biz/src/main/java/org/jeecg/modules/system/controller/CommonController.java
    start_line: 188
  - end_line: 70
    path: jeecg-boot/jeecg-module-system/jeecg-system-biz/src/main/java/org/jeecg/config/jimureport/JimuReportTokenService.java
    start_line: 43
  - end_line: 289
    path: jeecg-boot/jeecg-module-system/jeecg-system-start/src/main/resources/application-dev.yml
    start_line: 282
  - end_line: 360
    path: jeecg-boot/jeecg-module-system/jeecg-system-start/src/main/resources/application-dev.yml
    start_line: 333
  - end_line: 53
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/common/util/dynamic/db/FreemarkerParseFactory.java
    start_line: 49
  premise_ids: []
  review_status: unverified
  validation_level: source_analysis
  review_notes: |-
    出现新的上传目录消费面、执行组件调用方或脚本来源变化时，值得重新研究该连接。
    依赖字节码分析、写入前提和运行对照见 work_i037/NOTES.md，
    以及 work_i037/decomp/、work_i037/decomp2/、work_i037/evidence/。
    原始源码定位仅支持版本及仓库侧消费者，不替代上述依赖材料。
```

</details>

`validation_level: source_analysis` 是原快照值，原记录虽然还描述了运行观察，本附录不自行升级该字段。决定性依赖行为在字节码材料中；pom 和仓库源码位置只支持版本锚定及所查消费者，不能冒充全部依赖行为的源码证明。

完整分析和实验对照仍在 [work_i037/NOTES.md](/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/work_i037/NOTES.md) 及同目录的 `decomp/`、`decomp2/`、`evidence/`。研究节点负责告诉其他 Worker 这个方向被什么阻断，以及什么新信息值得重开。

## 6. i041：明确局部验证范围，不能预定义全链成功

```yaml
id: i041
from: [f001, f004, f021, f022]
description: |-
  检验 dev 部署下 MCP stdio 机制的实际后段效果，
  并确定它需要的有效身份、权限和配置条件。
  f004 提供机制线索，f021/f022 提供待核对的身份取得候选。

  区分后段机制是否成立，以及候选前段能否从固定契约允许的起点接入。
  用缺少身份、权限或配置条件的对照确定边界；
  局部效果成立时保留为条件性结论，不据此宣布整条目标路径成立。
```

这是对原 Intent 的编辑性纠正：删除“只差末端效果”和“某个效果出现即完整链成立”的预设，没有提前加入 f029 的实验结果，也没有把后来出现的 h009 放进此时上下文。

## 7. f029：保留真实后段效果，显式留下起点缺口

理想首段：**具有有效身份和所需权限、dev 配置启用 stdio 时，记录观察到容器内命令执行效果；匿名取得这一身份的合规前段尚未在本次实验独立闭合。**

<details open>
<summary>f029 的完整节点示例：全部原源码定位保留，仅去掉证据 hash</summary>

```yaml
id: f029
description: |-
  本地隔离实例中，持有效身份和 airag:mcp:save 权限的调用者，
  在 dev 配置允许 stdio 时，记录观察到应用容器内命令执行效果；
  无有效令牌、无该权限或禁用 stdio 的对照被拒绝。
  本次支持条件性后段效果，尚未独立建立符合固定 Goal 起点的完整取得过程。

  协议握手失败不否定已经发生的子进程效果；
  它也不能用于推断其他运行状态或配置下同样成立。
audit:
  conditions:
  - 该入口不是匿名入口；调用者已取得有效身份并具有 airag:mcp:save 权限。
  - 运行 profile=dev，allow-sensitive-nodes 包含 stdio；移除该项及使用不含该项的其他 profile 会改变结论。
  - 原记录的一条身份取得路线依赖仍可使用的默认凭据，不能省略为无条件的初始能力。
  - 另一条路线在本次实验中从实例内部存储取值，只验证了中间身份可驱动后段；外部取得过程未在本轮独立闭合。
  - 本地应用与数据服务共享网络命名空间，使默认回环地址可达；这属于实验拓扑，需要随结论保留。
  - 运行镜像与目标源码提交的构建对应关系未独立核验；相同文件摘要不足以证明整个构建来源。
  - 请求仍需满足接口字段类型要求；合法参数形态及对照记录见原始材料。
  - 仅观察到本地实例内的效果；实例新增了验证数据，固定源码未改，未评估容器外影响及全部后续状态。
  evidence:
  - end_line: 95
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/controller/AiragMcpController.java
    start_line: 82
  - end_line: 188
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/service/impl/AiragMcpServiceImpl.java
    start_line: 156
  - end_line: 182
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/service/impl/AiragMcpServiceImpl.java
    start_line: 178
  - end_line: 186
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/service/impl/AiragMcpServiceImpl.java
    start_line: 184
  - end_line: 34
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/entity/AiragMcp.java
    start_line: 32
  - end_line: 78
    path: jeecg-boot/jeecg-boot-module/jeecg-boot-module-airag/src/main/java/org/jeecg/modules/airag/llm/entity/AiragMcp.java
    start_line: 76
  - end_line: 23
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/AiRagConfigBean.java
    start_line: 15
  - end_line: 248
    path: jeecg-boot/jeecg-module-system/jeecg-system-start/src/main/resources/application-dev.yml
    start_line: 246
  - end_line: 110
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/ShiroConfig.java
    start_line: 110
  - end_line: 129
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/ShiroConfig.java
    start_line: 129
  - end_line: 56
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/filters/JwtFilter.java
    start_line: 49
  - end_line: 708
    path: jeecg-boot/jeecg-module-system/jeecg-system-biz/src/main/java/org/jeecg/modules/system/controller/LoginController.java
    start_line: 680
  - end_line: 88
    path: jeecg-boot/jeecg-boot-base-core/src/main/java/org/jeecg/config/shiro/ShiroRealm.java
    start_line: 65
  - end_line: 51
    path: jeecg-boot/jeecg-module-system/jeecg-system-biz/src/main/java/org/jeecg/modules/openapi/filter/ApiAuthFilter.java
    start_line: 47
  - end_line: 161
    path: jeecg-boot/jeecg-module-system/jeecg-system-biz/src/main/java/org/jeecg/modules/openapi/controller/OpenApiController.java
    start_line: 161
  - end_line: 320
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 300
  - end_line: 329
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 329
  - end_line: 8561
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 8561
  - end_line: 9816
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 9816
  - end_line: 9894
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 9894
  - end_line: 10756
    path: jeecg-boot/db/jeecgboot-mysql-5.7.sql
    start_line: 10755
  premise_ids: []
  review_status: unverified
  validation_level: source_analysis+runtime_reproduction
  review_notes: |-
    主要逐次记录：i041-work/evidence-i041.jsonl；相关脚本和环境记录在 i041-work/。
    材料包含身份、权限、配置门、协议错误及效果观察。
    本条不继承任何来源 Fact 为真来证明后段；来源用于定位。
    尚缺符合固定 Goal 起点的身份取得前段；默认凭据取得记录与内部取值实验
    不能据此省略为无条件能力，也不能将后段独立复现扩大为目标路径独立复现。
    握手失败后的回收时序和全部后续同步状态尚未确认。
```

</details>

原记录包含的既有凭据路线、内部存储取值、协议错误与逐项对照，仍在 [i041-work/evidence-i041.jsonl](/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/i041-work/evidence-i041.jsonl) 和原调用回执中。图中保留这些操作对结论的影响，不重复具体请求、响应、实验端口和全部执行步骤。

`premise_ids: []` 沿用原记录，且此处主张收敛为本轮观察到的条件性后段；它不意味着完整路径没有前提。若未来主张扩展到整条路径，就必须重新交代尚缺前段的证据或实际依赖。

## 8. h009：具体纠偏，不成为另一份总控提示词

```yaml
id: h009
creator: overseer
content: |-
  分享准入存在需要核对的冲突：本次字节码复核认为，
  无分享口令的记录仍按 status 判断放行；
  设有分享口令的记录须有匹配口令或有效共享 cookie。
  所查 processShareView 路径在口令缺失或不匹配时删除相关 cookie，且不签发。

  这与 i016-work/final.json 中“无需知道分享口令即可取得共享 cookie”的表述不一致。
  使用该前提的后续研究应先查清冲突，不能仅凭默认分享记录存在就认为准入已满足。
```

保留 `overseer` 来源身份。Hint 没有 `review_status`、`validation_level` 或 `premise_ids`，不新增这些字段；也不凭这个 Hint 自动把其他节点判成 refuted。此处描述的是旧运行已经发生的核对，不要求新版 Overseer 重做。新版只在 Intent 存在方向或条件问题时给出具体 Hint；需要形成新技术结论时，由后续研究保存 Fact，候选证明交独立终审。

## 9. planning_notes：只保留研究决策

以下是对 [08:14:54Z Reason 规划记录](/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/research/generation-001/reason-memory-07b25e9cdb4f4357bb7449f79301d03f.json) 的理想改写。它修正原文中扩大 f025 否定范围和提前收敛起点缺口的表述，不代表原运行当时已经作出这些正确判断。

```yaml
planning_notes: |-
  暂搁现有写入到加载/执行的连接：f025 在所查依赖和消费者范围内给出阻断。
  出现新的消费面、执行调用方或脚本来源变化时再重开；不扩大为项目没有执行路径。

  i039、i041、i042 已分别承担不同的后段连接验证，暂不新增同题任务。
  身份取得候选仍须与固定起点约束对齐；后段成功不能补掉这项缺口。
  下一轮结合这些任务的新观察，决定继续验证前段、寻找其他连接或暂搁相应方向。
```

该时点没有 f029 结果，也没有 h009。回读仅需这段规划、必要的时间与原回执定位；generation、各类 digest 和高精度排序信息由程序保留。

## 10. 这些节点如何出现在导航中

下面是同一快照的选段，不是完整图；未列出的来源节点和其他开放任务仍可从完整图读取。这里不虚构 `graph.yaml` 行号，实现时由导出器为本次快照填入节点块的真实范围。

| ID | 导航文案 | 状态／来源 | 全文定位内容 |
| --- | --- | --- | --- |
| origin | JeecgBoot 3.9.5，scan 1；旧任务结果仅作线索 | 起点 | origin 完整块 |
| goal | 完整目标见本次调用固定契约 | 目标锚点 | goal 锚点 |
| f025 | 所查版本和配置下，受限写入尚不能接到所查执行面；范围外路径未排除 | unverified；premises=[] | f025 含完整 conditions／notes 的块 |
| f029 | 有效身份及权限、dev 开启 stdio 时观察到后段效果；允许起点未闭合 | unverified；premises=[] | f029 含完整 conditions／notes 的块 |
| h009 | 分享准入口径存在冲突，依赖此前提的方向需核对 | overseer | h009 完整块 |

此快照中 i037、i041 已完成并分别指向 f025、f029，不能再次作为 Open Intents 展示或派发。它们只在需要复盘来源时读取。生产导航的 Open Intents 部分须列出真实未完成任务，此选段没有代替该部分。

## 11. 同一材料对不同角色意味着什么

| 调用场景 | 主动交付 | 按需读取 | 本例不会交付的无关全文 |
| --- | --- | --- | --- |
| 创建 i037 后的 Explore | 固定契约、i037全文、f015/f016入口、当时的Hint导航 | 来源完整条件、相关消费者、依赖材料与源码 | 所有其他Intent、尚未产生的f025 |
| 创建 i041 后的 Explore | 固定契约、i041全文、f001/f004/f021/f022入口、调用时已存在的Hint导航 | 后段机制与前段候选的必要证据 | 预写的f029结果、完整全图报告 |
| 08:14的Reason | 固定契约、当时全图导航、已有规划、必要失败简述 | f025及其他会改变连接判断的节点 | Open Intents第二份全文、稍后的h009/f029 |
| 新版Overseer观察未结i041（流程示意，非历史调用） | 固定契约、i041全文及来源ID、全图／Hint导航 | f001/f004/f021/f022的条件或与方向有关的既有材料 | 不逐条复核来源Fact，不提前看到尚未产生的f029 |
| 后续Closing／Final Review | 依主方案提供全图候选入口或完成申请及依赖闭包，unverified可进入终审 | 终审独立核验候选及必要前提，查阅反证、未解条件和原材料 | 不等待逐条Fact认证，不把“引用匹配”作为结论成立 |

新版对未结 i041 的合理纠偏可以是：“后段效果只能证明条件性能力；来源中的身份取得候选仍需满足固定起点，不能预设全链成功。”正常方向则不发 Hint。这里不新增真实 Hint ID、不改变 i041 的历史完成状态，也不把此建议当成新技术核验。

快照中没有完成申请，因此不伪造一份 successful Complete 或通过的终审数据。Closing／Final 行仅说明未来输入范围，不声称已有对应真实调用结果。

## 12. 改写前后究竟少了什么

| 节点 | 原正文字数 | 示例正文字数 | 留在节点中的重点 |
| --- | ---: | ---: | --- |
| i037 | 1,030 | 164 | 连接问题与判别观察 |
| f025 | 4,982 | 165 | 有范围的阻断与关键机制 |
| i041 | 2,120 | 175 | 后段验证与前段缺口分开 |
| f029 | 4,634 | 187 | 条件性效果和起点限制 |
| h009 | 310 | 223 | 具体冲突与需要核对的前提 |

这些差值只比较 `description`／`content` 本身，按字符计数（含换行），不是 token 数；不包含完整 conditions、证据定位和按需材料，也不代表质量提升比例。文字改写还纠正了原主张范围，并非可以自动执行的无损压缩。

减少的是：重复的全局规则、前序长报告、全部实验步骤、机器指纹，以及未经证明的全链成功定义。保留的是：可引用主张、关键条件、具体阻断、未建立部分、研究来源和真实证据入口。新角色不必从长报告中重新提炼这些内容，但仍可以沿入口回看全部材料。

## 13. 回查原始记录

- [i037 原始调用回执](/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/research/generation-001/explore_execute-c1ac0978955741cfa4b40df3524a0ca8.json)：确认 `[f015, f016]` 来源与 f025 研究过程。
- [i041 原始调用回执](/Users/opr4/Desktop/cairn-runs/composition-real-001/service/cairn-service/work/proj_001/research/generation-001/explore_execute-fe1de4bfbc584eeaa09bdd7cc731e222.json)：确认 `[f001, f004, f021, f022]` 来源与 f029 研究过程。
- [归档快照](/Users/opr4/Desktop/cairn+kritt/.worktrees/cairn-compositional-part4-side/.claude/verification/live-review-8002-20261015-162743/snapshot.json)：原节点正文、状态、条件、证据与 h009。
- [研究效果只读复盘](/Users/opr4/Desktop/cairn+kritt/.worktrees/cairn-compositional-part4-side/.claude/verification/live-review-8002-20261015-162743/audit-effect/result.md)：连接检验、反证回流与主张边界的分析依据。

本机运行材料仅作为案例来源，不作为产品部署依赖。本文没有改写这些原件，也没有重新核实其技术结论。
