# Builder：准备审计方案（R-CP1 中间态）

2026-10-10：工作区已完成 [v0.5](specs/kritt-audit-reconnect-mvp.md) 的 R-CP1 开发，并获用户提交授权。Builder 通过同一 harness session 自主查询 Kritt、读取源码并生成 AuditPlan；R-CP2 的 audit 初始化、监督和研究尚未接通。

当前 `cairn builder` 命令生成方案后打印 `plan_ready` 和产物路径，然后以非零退出码明确提示“audit 接回尚未完成”。不会启动 Server、Dispatcher 或创建 generic 项目。开发验收直接使用 `run_builder()`，其成功结果为 `plan_ready`。完整运行脚本见本地 R-CP1 交接材料。

下面保留 v0.3 已交付版本的历史准备、研究与结果说明；其中“自动启动研究”和 completed 描述不适用于本次 R-CP1 中间态。现有源码型 audit 入口及普通项目仍可单独使用。

## 首次准备

在Cairn仓库根目录执行：

```sh
uv sync --project cairn --python 3.13
cp dispatch.builder.example.yaml dispatch.builder.yaml
```

本机`claude`应已登录并可调用。示例仅启用claudecode，复用本机模型配置；不需要另建模型SDK，也不需要为Cairn安装Docker。Kritt数据库可以继续运行在原有Docker环境中。

编辑`dispatch.builder.yaml`中的目标配置，沿用B-CP1已准备的源码和manifest：

| 配置 | 填写内容 |
|---|---|
| `targets.zentao.repo_full` | Kritt中的仓库名，示例为`easysoft/zentaopms` |
| `scan_id`、`commit` | 示例固定scan19与对应commit；使用新扫描时同步选择其源码。可删去这两项以按仓库选最新已完成扫描 |
| `source_root` | 实际完整源码目录 |
| `source_manifest` | 该源码的外置manifest；格式和准备条件见[Kritt探针指南](kritt-probe.md) |

上述target字段位于`controller.kritt.targets`。相对文件路径以配置文件所在目录为基准。源码应实际可读；使用映像或挂载卷时先挂载。切换最新scan不会自动下载或切换本地源码，源码仍须对应所选扫描。

在当前终端设置`CAIRN_KRITT_READONLY_DSN`为已有SELECT连接，或通过环境配置加载它。示意如下，账号和地址使用自己的值，密码通过本机连接配置提供，不写入仓库文件：

```sh
export CAIRN_KRITT_READONLY_DSN='host=127.0.0.1 port=5433 dbname=open_kritt user=YOUR_READONLY_USER'
```

连接地址使用数字IP、Unix socket，或同时指定主机名和预解析`hostaddr`；首版不自行解析DNS。连接与目标配置完成一次后，日常运行只需下面一条命令。

## 一条命令运行

配置沿用Cairn现有的单次调用时限：专用示例的Builder和Explore为300秒，Reason为120秒。可以按实际模型耗时调整这些已有字段；本入口没有额外的准备累计额度或项目总预算。出错时保留方案、材料及错误说明，操作者重新发起。

Builder在每次get后保存完整分页材料，下一轮通过材料文件继续组织方案，源码探索交给研究Worker。

运行时启动本次专用的本地Server，配置中的端口需要空闲；`server: http://127.0.0.1:0`可以自动选择空闲端口。研究使用普通项目的`runtime.prompt_group`，不接旧audit模式的审核流程。

```sh
uv run --project cairn cairn builder --config dispatch.builder.yaml --target zentao --scan-id 19 \
  --goal '研究已安装业务态、默认配置下的匿名远程代码执行' \
  --run-dir /absolute/path/to/new-run
```

`--scan-id` 可以省略，这时按该目标已配置的已完成扫描选取。终端如果还缺开工所需的信息，会在同一次运行里提问。

无需先手动运行`serve`或`dispatch`；命令会启动本次独立Server，完成初始图后启动Dispatcher。`--run-dir`使用源码目录之外的新目录，重跑时换一个目录。需要回放已保存的Kritt材料时可显式加`--view /path/to/view.json`，这种方式不访问在线Kritt数据库。

终端输出本次Server地址、项目ID、运行目录及`project_url`图页面直达链接。运行中仅在事实数或待研究Intent数变化时输出进度；`fact_count`包含导入种子与新研究事实，不含origin/goal，`open_intent_count`是尚无结论的Intent数量。

初始图准备完后才启动Dispatcher；打开`project_url`即可在现有Cairn页面查看图。Reason完成项目或用户通过已有停止操作结束后退出；Ctrl+C也会停止本次项目。结束时先通过现有接口保存结果，再回收本次服务。终端最后的`results`给出保存路径；导出错误会显示原因，不影响本次进程清理。

## 查看与保存结果

| 文件 | 内容 |
|---|---|
| `plan.json` | `title`、`origin`、`goal`、`seeds`、`intents`、可选 `hints` |
| `selected-materials.json` | 本次 `get` 完整读过的记录，供后续Builder调用和研究Worker读取 |
| `run.json` | scan、commit、源码路径和方案位置 |
| `project.json` | 普通项目ID、seed key到实际Fact ID的映射、研究Intent及工作目录 |
| `research.json` | 本次运行地址、图页面链接、状态、数量、结果路径与服务进程信息 |
| `result.json` | 结束时的项目详情，含真实项目状态、Facts、Intents和Hints |
| `graph.yaml` | 现有YAML导出接口生成的研究图 |
| `timeline.txt` | 现有时间线导出接口生成的过程记录 |
| `cairn.db` | 本次普通项目与研究图 |
| `server.log`、`dispatcher.log` | 本次服务与研究执行日志 |
| `error.json` | Builder准备失败原因；研究运行和导出错误见`research.json`及对应日志 |

Worker在项目工作目录读取`repo/`、`kritt-materials.json`和`research-context.json`。种子是导入的既有分析，新的Explore结果才是本次研究产出。普通项目的`completed`表示研究结束，不表示已独立审定漏洞。

本次Server退出后，可直接阅读三个结果文件；若要再次使用现有图页面，只启动读取该运行数据库的Server即可：

```sh
uv run --project cairn cairn serve --db-path /absolute/path/to/run/cairn.db --port 8000
```

然后打开`http://127.0.0.1:8000/#/projects/proj_001`，项目ID使用本次输出中的实际值。这只展示已有结果，无需重跑Builder或启动Dispatcher。

## 提示词和取材预设

Builder使用包内[builder.md](../cairn/src/cairn/controller/prompts/builder.md)，按“提问、查询、完成方案”返回结果。查询字段与RCE/信息泄露机制词来自[kritt_tools.py](../cairn/src/cairn/controller/kritt_tools.py)。研究使用`runtime.prompt_group: default`的现有Explore/Reason提示词，不调用旧audit模式的预审、监视或终审。

可以先用明确的小目标体验完整链路，例如“读取module/install/control.php中构造函数的进入和终止条件，列出表达式与行号，仅做静态源码阅读”。实际漏洞目标可能需要更长研究，用户可通过图页面的停止操作或Ctrl+C结束。普通`completed`表示Agent认为目标已完成，`stopped`表示研究被停止，二者均会保存当时的图和时间线。
