# Cairn 二次开发记录

本 Fork 以 [oritera/Cairn](https://github.com/oritera/Cairn) 为上游，保留原始提交历史和署名。二开开发起于 2026-10-05；本次发布整理日期为 2026-10-16。

## 历史保留方式

- 原版开发基线：`8e7e0ea67552383851dfcabfba0c4e9c8d007878`。
- 已有 29 个二开提交保持原 SHA、作者、时间和父提交关系，没有 squash、rebase 或重写。
- 节点来源追溯沿用本地原始提交 `2e196f9`；本次仅追加首页与发布说明。
- 工作树中的运行数据库、本机配置、凭据、模型原始日志和临时验证目录不入库。
- 历史 CP/B-CP/R-CP/REC-CP 提交保留其原名称；当前能力以根目录 README 和当前代码为准，历史方案不等于尚需新增的流程。

## 已完成阶段

| 阶段 | 结果 |
| --- | --- |
| 审计控制器 CP1–CP6 | 统一 Agent 调用、审计数据协议、初始建图、探索、独立终审、旁路监督与时间预算 |
| Kritt Builder B-CP1–B-CP5 | 只读数据接入、自然语言开题、计划生成、研究启动和结果保存；保留历史 generic 兼容入口 |
| Harness 与原生 Builder | Grok Build、Claude Code 原生 Skill、共享 Driver 和角色资源池 |
| 审计接回 R-CP1–R-CP3 | Kritt 材料进入 audit 研究，整合研究、结案及独立验收 |
| 恢复 REC-CP1–REC-CP4 | 人工服务、在线提交、暂停计时、原项目恢复、运行面板和 Skill 控制 |
| 组合研究第1–4部分 | 固定 Kritt 查询入口、图导航、Reason 规划记忆、组合研究提示词和隔离样例 |
| 上下文与旁路优化 | 固定契约集中交付、按角色读取、Intent-only 异步 Hint 纠偏；保留独立终审 |
| Pi / Codex / MiniMax | 原生 CLI 协议、明确会话续接、错误终态与思考前缀处理；复用本机模型配置 |
| 图节点来源 | harness/Worker 持久记录、历史精确映射、详情展示、回放和离线处理 |

## 按实际 Git 历史排列的二开提交

| 日期 | 提交 | 原始提交说明 |
| --- | --- | --- |
| 2026-10-05 | [`73d2c70`](https://github.com/Opr4Mp3r/Cairn/commit/73d2c700226937ff0c4f156e92c14a171e8ce5ed) | feat: add shared controller agent execution for CP1 |
| 2026-10-05 | [`5f43d55`](https://github.com/Opr4Mp3r/Cairn/commit/5f43d557363cd803c794e7906078be4c4b9273b6) | feat: add audit data protocol for CP2 |
| 2026-10-06 | [`8c8be20`](https://github.com/Opr4Mp3r/Cairn/commit/8c8be203f42de0dbaf563546a46a5262fdec5349) | feat: build local audit tasks and run audited exploration for CP3 |
| 2026-10-06 | [`574159b`](https://github.com/Opr4Mp3r/Cairn/commit/574159bcfa90fe629786dadc262d17e7a9418735) | feat: add independent audit oversight and completion review for CP4 |
| 2026-10-06 | [`4fa1f7b`](https://github.com/Opr4Mp3r/Cairn/commit/4fa1f7bc8342e8c636a3bcdb5214122869d66e48) | feat: add finite audit budgets and resumable closing for CP5 |
| 2026-10-06 | [`a2c64d9`](https://github.com/Opr4Mp3r/Cairn/commit/a2c64d9e7ff8a93d2170bad7d1da0d6193cbf770) | feat: add local audit launcher and MVP delivery for CP6 |
| 2026-10-07 | [`1517a31`](https://github.com/Opr4Mp3r/Cairn/commit/1517a311c84cff10558f2e7c90c602bc40210c15) | feat: add Kritt read-only data and agent probe for B-CP1 |
| 2026-10-07 | [`8c36a42`](https://github.com/Opr4Mp3r/Cairn/commit/8c36a422d587893e088cda4aada023a9c125a131) | feat: generate a Kritt research plan from a natural-language goal |
| 2026-10-08 | [`d553493`](https://github.com/Opr4Mp3r/Cairn/commit/d553493214b81338c00da722e1f45b5ceb2d89d4) | feat: start generic Kritt research for B-CP3 |
| 2026-10-08 | [`1bbd0be`](https://github.com/Opr4Mp3r/Cairn/commit/1bbd0bed9b431fdd5c13a0421c6828804e284b28) | feat: show and save Kritt research results for B-CP4 |
| 2026-10-08 | [`c3c5122`](https://github.com/Opr4Mp3r/Cairn/commit/c3c5122f3e56ba346aef71a441f5ecedb1527135) | feat: deliver Kritt Builder MVP setup and guide for B-CP5 |
| 2026-10-08 | [`2dd2d3b`](https://github.com/Opr4Mp3r/Cairn/commit/2dd2d3b19eca07a04b4e88529253069a534b45c1) | feat: add Grok Build as a worker harness |
| 2026-10-10 | [`cf749c5`](https://github.com/Opr4Mp3r/Cairn/commit/cf749c5b57b2e64f5b01717a454f37bb82f1bad3) | feat: 实现 R-CP1 的 harness 审计开题与只读取材 |
| 2026-10-10 | [`17b6d72`](https://github.com/Opr4Mp3r/Cairn/commit/17b6d722839dddad09d44c962b7d658215216bda) | feat: 接通 R-CP2 的 Kritt audit 研究与旁路监督 |
| 2026-10-10 | [`02d4a3d`](https://github.com/Opr4Mp3r/Cairn/commit/02d4a3ddd0bbe04dbc8193ae4e6830b5d2ee7cb8) | feat: 完成 R-CP3 独立结案与审计结果交付 |
| 2026-10-10 | [`8306eac`](https://github.com/Opr4Mp3r/Cairn/commit/8306eaccab48ce91da66a70f440ed0bd2680d35d) | feat: 交付原生 Builder Skill 与角色资源池 |
| 2026-10-12 | [`567fc60`](https://github.com/Opr4Mp3r/Cairn/commit/567fc603df2847a38a2e6df32537a817a955f267) | docs: 固化任务恢复 v0.2 与人工服务边界 |
| 2026-10-12 | [`6e49058`](https://github.com/Opr4Mp3r/Cairn/commit/6e490587459a92530b9630ac1268fe7fae48795f) | feat: 完成 REC-CP1 人工服务与在线提交 |
| 2026-10-12 | [`ba0833f`](https://github.com/Opr4Mp3r/Cairn/commit/ba0833f5a0e468a20f1594b262861433d2064a27) | fix: 修复 REC-CP1 四项验收缺陷 |
| 2026-10-13 | [`8182f91`](https://github.com/Opr4Mp3r/Cairn/commit/8182f9104d6ad237932d51b0cf48fcef30808510) | feat: 完成 REC-CP2 暂停计时与原项目恢复 |
| 2026-10-13 | [`af94e31`](https://github.com/Opr4Mp3r/Cairn/commit/af94e3154fd6aeb189516e64313aad5b63ca2a8a) | feat: 完成 REC-CP3 在线控制与运行面板 |
| 2026-10-13 | [`e375a55`](https://github.com/Opr4Mp3r/Cairn/commit/e375a55ae02ae427cef2c8bc09f29ce5041cac86) | feat: 完成 REC-CP4 原生 Skill 任务控制 |
| 2026-10-15 | [`fb6a44e`](https://github.com/Opr4Mp3r/Cairn/commit/fb6a44e07656a8b766e9dbc50b512d613803d99f) | feat: 为审计研究交付固定 Kritt 查询入口 |
| 2026-10-15 | [`03ee9a0`](https://github.com/Opr4Mp3r/Cairn/commit/03ee9a064b3c6960811db5d2d64324e0c6ab4f0d) | feat: 增加审计图导航与 Reason 规划记忆 |
| 2026-10-15 | [`c7a5db7`](https://github.com/Opr4Mp3r/Cairn/commit/c7a5db7601b6600e92d12c4cbf157a74e2d19a9a) | feat: 对齐组合研究提示词与原生 Builder Skill |
| 2026-10-15 | [`61217c9`](https://github.com/Opr4Mp3r/Cairn/commit/61217c9c683451963e2e8722e88ddb236e0722eb) | feat: 完成组合研究隔离样例与 Claude Code 结果适配 |
| 2026-10-16 | [`cb05e82`](https://github.com/Opr4Mp3r/Cairn/commit/cb05e8216926fa747f987fa7ed23c1e1733f5ff6) | feat: 优化审计上下文与 Intent 旁路监督，完善 Pi/Codex 接入 |
| 2026-10-16 | [`df387ec`](https://github.com/Opr4Mp3r/Cairn/commit/df387eca71186628fc52bac997fdc17510b19fdd) | feat: 接入 MiniMax Code 本机 harness 与角色资源池 |
| 2026-10-16 | [`2e196f9`](https://github.com/Opr4Mp3r/Cairn/commit/2e196f9b4fc005cf0e48da64ef4dcf9172a88842) | feat: 为图节点记录并展示 harness 来源 |

## 验证说明

来源追溯代码快照的完整 Python 回归记录为 803 通过、2 跳过、6 个已有警告；前端行为为30项通过。两项跳过是需显式启用的既有真实 Claude 组合研究样例。此前各阶段的开发验证保留在本地工作材料中，没有将含个人路径或运行输出的原始日志上传。

真实 harness 验证覆盖临时材料读取、会话续接，以及部分并发调用；未把这些小样例称为真实目标漏洞发现验收。已知模型内容偏差和未实现能力见 [README](README.md#验证与边界)。
