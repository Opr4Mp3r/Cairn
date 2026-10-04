from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.dispatcher.runtime.process import ProcessResult


class FileReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str


class AgentCallResult(BaseModel):
    role: str
    worker: str
    phase: str
    workspace: str
    session: str | None
    prompt: str
    status: Literal["success", "failed", "timed_out", "cancelled", "invalid_output"]
    process: ProcessResult
    response: str | None = None
    data: dict[str, Any] | None = None
    error: str | None = None
    elapsed_seconds: float
