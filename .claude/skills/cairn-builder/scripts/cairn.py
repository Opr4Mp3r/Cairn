#!/usr/bin/env python3
"""Prepare and control tasks through the repository's HTTP-only client commands."""
import os
from pathlib import Path
import sys

commands = ("prepare", "submit", "status", "pause", "resume")
if len(sys.argv) == 1 or sys.argv[1] in ("--help", "-h"):
    print("用法：cairn.py {" + ",".join(commands) + "} [参数]\n系统服务由用户在终端手动启动。")
    sys.exit(0)
if sys.argv[1] not in commands:
    sys.exit("此 Skill 只准备、提交、查询、暂停或恢复任务；系统服务由用户手动管理。")

repo = Path(__file__).resolve().parents[4]
python = repo / "cairn" / ".venv" / "bin" / "python"
if not python.is_file():
    sys.exit(f"缺少项目虚拟环境，请在 {repo} 执行 uv sync --project cairn")
os.chdir(repo)
os.execv(str(python), [str(python), "-c", "from cairn.cli import main; main(prog_name='cairn')", *sys.argv[1:]])
