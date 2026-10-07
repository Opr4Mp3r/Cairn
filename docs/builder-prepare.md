# B-CP2：从需求生成研究方案

版本以 `docs/specs/kritt-builder-mvp.md` v0.3 为准。这一步只生成方案，不创建 Cairn 项目，也不做预审、种子复核、来源封印或跨进程恢复。

配置沿用 B-CP1 的目标仓库、源码和只读连接。单次模型调用使用已有的 `controller.timeouts.build`（示例里是 180 秒）。一次 claudecode 进程若会读源码，把这个已有时限调大即可，不要另加准备阶段的累计额度。出错时保留 `error.json` 和已经取到的材料，操作者重新发起。

```sh
uv run --project cairn cairn builder --config config.yaml --target zentao --scan-id 19 \
  --goal '研究已安装业务态、默认配置下的匿名远程代码执行' \
  --run-dir /absolute/path/to/new-run
```

`--scan-id` 可以省略，这时按该目标已配置的已完成扫描选取。终端如果还缺开工所需的信息，会在同一次运行里提问。

输出：

| 文件 | 内容 |
|---|---|
| `plan.json` | `title`、`origin`、`goal`、`seeds`、`intents`、可选 `hints` |
| `selected-materials.json` | 本次 `get` 完整读过的记录 |
| `run.json` | scan、commit、源码路径和方案位置 |
| `error.json` | 失败原因；成功时不写 |
