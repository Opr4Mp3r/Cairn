#!/usr/bin/env python3
"""Run the repository's Cairn CLI from any working directory."""
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[4]
python = repo / "cairn" / ".venv" / "bin" / "python"
if not python.is_file():
    sys.exit(f"缺少项目虚拟环境，请在 {repo} 执行 uv sync --project cairn")
os.chdir(repo)
os.execv(str(python), [str(python), "-c", "from cairn.cli import main; main(prog_name='cairn')", *sys.argv[1:]])
