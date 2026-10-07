# Builder：从需求生成方案并启动研究

版本以 `docs/specs/kritt-builder-mvp.md` v0.3 为准。Builder生成方案后创建普通Cairn项目，导入种子和研究Intents，准备源码/材料目录，再启动本次Dispatcher。

配置沿用 B-CP1 的目标仓库、源码和只读连接。单次模型调用使用已有的 `controller.timeouts.build`（示例里是 180 秒）。一次 claudecode 进程若会读源码，把这个已有时限调大即可。出错时保留方案、材料及错误说明，操作者重新发起。

`source_root`需要指向实际可读的源码目录；使用已准备的映像或挂载卷时，运行前先挂载。Builder在每次get后保存完整分页材料，下一轮通过材料文件继续组织方案，源码探索交给研究Worker。

运行时启动本次专用的本地Server，配置中的端口需要空闲；`server: http://127.0.0.1:0`可以自动选择空闲端口。研究使用普通项目的`runtime.prompt_group`，不接旧audit模式的审核流程。

```sh
uv run --project cairn cairn builder --config config.yaml --target zentao --scan-id 19 \
  --goal '研究已安装业务态、默认配置下的匿名远程代码执行' \
  --run-dir /absolute/path/to/new-run
```

`--scan-id` 可以省略，这时按该目标已配置的已完成扫描选取。终端如果还缺开工所需的信息，会在同一次运行里提问。

终端输出本次Server地址、项目ID、运行目录及研究状态。初始图准备完后才启动Dispatcher；可在现有Cairn页面查看研究图。Reason完成项目或用户通过已有停止操作结束后退出；Ctrl+C也会停止本次项目并回收本次服务。

输出：

| 文件 | 内容 |
|---|---|
| `plan.json` | `title`、`origin`、`goal`、`seeds`、`intents`、可选 `hints` |
| `selected-materials.json` | 本次 `get` 完整读过的记录，供后续Builder调用和研究Worker读取 |
| `run.json` | scan、commit、源码路径和方案位置 |
| `project.json` | 普通项目ID、seed key到实际Fact ID的映射、研究Intent及工作目录 |
| `research.json` | 本次运行地址、状态、日志与服务进程信息 |
| `cairn.db` | 本次普通项目与研究图 |
| `server.log`、`dispatcher.log` | 本次服务与研究执行日志 |
| `error.json` | 失败原因；成功时不写 |

Worker在项目工作目录读取`repo/`、`kritt-materials.json`和`research-context.json`。种子是导入的既有分析，新的Explore结果才是本次研究产出。普通项目的`completed`表示研究结束，不表示已独立审定漏洞。查看和导出结果的进一步整理由B-CP4完成。
