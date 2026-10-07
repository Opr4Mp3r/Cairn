# B-CP1：Kritt 只读取材探针

此入口验证 PostgreSQL → Kritt 材料 → 同版本源码 → 结构化观察。它复用 Cairn 的 WorkerDriver 和执行器，默认使用 claudecode；运行不会创建项目，不需要启动 Cairn Server/Dispatcher。B-CP1已完成，提交为`1517a31`；后续需求对话、方案生成及项目启动按[正向MVP计划](specs/kritt-builder-mvp.md)推进。

## 准备

1. 使用项目已有的 Python/uv 环境：`uv sync --project cairn --group dev --python 3.13`。本机 `claude` 应已经能执行任务。
2. 复制根目录的 `dispatch.kritt-probe.example.yaml`，设置目标仓库、可选 scan/commit、完整源码目录及外置 manifest。源码和 manifest 的相对路径以配置文件所在目录为基准。
3. 在运行终端配置 `CAIRN_KRITT_READONLY_DSN`（或 `dsn_env` 指定名称），使用已准备好的只读连接；配置文件和命令行均不包含密码。账号需要必要表的 SELECT；程序另用短 `REPEATABLE READ READ ONLY` 事务固定资料。

首版连接必须显式使用数字IP（如`host=127.0.0.1`）、Unix socket目录，或预解析的`hostaddr`。需要保留TLS主机名时可设置`host=db.example hostaddr=192.0.2.10`，保留原host参与TLS/密码文件匹配。数字IP会显式传入hostaddr，避免解析线程；单独主机名、空地址项、非数字hostaddr以及连接service会在建连前报错。PGHOST/PGHOSTADDR环境回退同样校验，不能用service文件绕过。预解析地址由部署配置提供，不在探针内执行DNS，以保证期限/取消退出有界。

资料适配为显式 `kritt_v1` profile：验证入口/Flow/finding的字段与来源层关系，不假定步骤ID总是1/2/3。找不到指定scan、版本不匹配、来源断链、未知schema或读取超限会报错，不能静默改选其他资料。

源码 manifest 采用 format_version=1，包括 repo_full、commit_sha、git_tree、archive_sha256、tracked_count、files（相对路径→SHA256），以及可选 path_aliases/path_mappings。它必须放在源码目录外，并由受信操作者从指定commit的完整归档生成；文件哈希校验不替代导出来源证明。源码缺文件、多文件、改动、符号链接均会被拒绝。真实禅道归档存在大小写有别的路径，解包目录需能完整保留它们。

## 运行

```sh
uv run --project cairn cairn kritt-probe \
  --config dispatch.kritt-probe.example.yaml --target zentao --scan 19 \
  --goal "从安装入口的输入处理记录中选择一条材料，核对对应源码及必要条件" \
  --output /absolute/path/to/new-probe-run
```

`--output` 必须是新目录，且位于目标源码外。指定scan覆盖配置中的scan；未指定时使用配置选择或最新已完成匹配scan。该探针只需选一个相关分支，搜索后由程序完整读取get分页，随后模型读取材料文件和实际源码。分页同样消耗查询额度，错误/格式修复消耗调用额度；时间与次数有限。Ctrl+C/SIGTERM会取消本次调用。

离线回归可显式加 `--view /path/to/view.json`。这只用于夹具/复跑，不能冒充在线PG验证。线上默认自动获取并保存视图，用户无需手工导出。

## 输出与含义

| 文件 | 内容 |
|---|---|
| view.json | 单scan资料视图、profile与digest |
| source-baseline.json | 完整源码指纹与外置manifest信息 |
| query-receipts.json | 实际查询、分页、返回引用、完整读取ledger |
| selected-materials.json | 已完整展开的所选材料及关联，供模型跨调用读取 |
| call-*/input.json、result.json | 每次共享harness输入、原始返回、session、耗时与执行结果 |
| call-*/tool-result-*.json | 每页实际返回；包含分片/截断状态 |
| summary.json 或 failure.json | 成功观察，或失败类型与已消耗额度 |

成功会核对引用身份、源码文件hash、行号与完整行引文；观察仍是 unverified。它不证明漏洞、匿名可达性、默认部署或Goal达成。Kritt的rank/canonical/valid/scope是分析标签；执行失败、stub与无finding均不等于安全结论。

只读DSN会从Agent子进程环境过滤。真实产物和调用原文可能包含目标资料，保存在本地运行目录，不提交Git。本文保留已实现探针的用法；后续方案生成见[Builder准备入口](builder-prepare.md)，不增加独立预审或种子复核。
