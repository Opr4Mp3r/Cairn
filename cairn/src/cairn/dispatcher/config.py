from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


TaskType = Literal["reason", "explore", "bootstrap"]
PoolRole = Literal["reason", "explore", "bootstrap", "build", "preflight", "seed_review", "overseer", "closing", "final_review"]
WorkerType = Literal["claudecode", "codex", "pi", "grok", "mock"]
RolePools = dict[PoolRole, Annotated[dict[WorkerType, Annotated[int, Field(strict=True, gt=0)]], Field(min_length=1)]]
CompletedAction = Literal["remove", "stop"]
WorkerHealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]
ExecutionMode = Literal["container", "local"]
LocalCompletedAction = Literal["keep", "remove"]

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "claudecode": (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "codex": (
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
    ),
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "grok": (
        "GROK_MODEL",
        "GROK_BASE_URL",
        "XAI_API_KEY",
    ),
    "mock": (),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "explore_conclude.md": ("{intent_id}", "{intent_description}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
}

PROMPT_REQUIRED_TOKENS_BY_GROUP: dict[str, dict[str, tuple[str, ...]]] = {
    "audit": {
        "reason.md": ("{graph_yaml}", "{fact_ids}", "{max_intents}", "{audit_context}"),
        "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{intent_sources}", "{audit_context}"),
        "explore_conclude.md": ("{intent_id}", "{intent_description}", "{intent_sources}", "{audit_context}"),
        "bootstrap.md": ("{origin}", "{graph_yaml}", "{intent_id}", "{intent_description}", "{intent_sources}", "{hints}", "{audit_context}"),
        "bootstrap_conclude.md": ("{origin}", "{graph_yaml}", "{intent_id}", "{intent_description}", "{intent_sources}", "{hints}", "{audit_context}"),
    },
    "mock": {
        "reason.md": ("{fact_ids}", "{open_intents}", "{max_intents}"),
        "explore.md": ("{intent_id}",),
        "explore_conclude.md": ("{intent_id}",),
        "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
        "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
    }
}

MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "healthcheck": frozenset({"ok", "fail"}),
    "reason": frozenset({"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_execute": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap": frozenset({"complete", "fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
}

MOCK_DEFAULT_BEHAVIOR: dict[str, dict[str, Any]] = {
    "healthcheck": {
        "delay": [0.05, 0.15],
        "outcomes": {"ok": "1.0", "fail": "0.0"},
    },
    "reason": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "0.0",
            "intent": "1.0",
            "noop": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_execute": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "1.0",
            "fact": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
}

MOCK_ALLOWED_ENV_KEYS = frozenset(
    {f"MOCK_{phase.upper()}" for phase in MOCK_ALLOWED_OUTCOMES}
)


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig


class ContainerConfig(BaseModel):
    image: str
    network_mode: str
    completed_action: CompletedAction
    cap_add: list[str] = Field(default_factory=list)


class LocalConfig(BaseModel):
    workspace_root: str | None = None
    completed_action: LocalCompletedAction = "keep"
    excluded_env: list[str] = Field(default_factory=list)


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    worker_healthcheck: WorkerHealthcheckMode = "startup_only"
    execution: ExecutionMode = "container"
    prompt_group: str = Field(min_length=1)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: WorkerType
    task_types: list[PoolRole]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[PoolRole]) -> list[PoolRole]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        # Required LLM env keys (base_url / key / model) are enforced per execution mode by
        # DispatchConfig: container mode needs them, local mode reuses the host CLI config.
        # The checks below are mode-independent and always apply.
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        if self.type == "grok":
            from cairn.dispatcher.workers.adapters.grok import validate_grok_env

            validate_grok_env(self.name, self.env)
            _validate_optional_positive_int_env(self.name, self.env, "GROK_MODEL_CONTEXT_WINDOW")
        if self.type == "mock":
            resolve_mock_behavior(self.name, self.env)
        return self


class ControllerRoles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    builder: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)


class ControllerTimeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build: int = Field(default=180, gt=0)
    preflight: int = Field(default=120, gt=0)
    seed_review: int = Field(default=120, gt=0)
    overseer: int = Field(default=90, gt=0)
    closing: int = Field(default=60, gt=0)
    final_review: int = Field(default=60, gt=0)


class ControllerReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, gt=0)


class ControllerBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_seconds: int = Field(default=1800, gt=0)
    drain_seconds: int = Field(default=120, gt=0)
    final_review_seconds: int = Field(default=360, gt=0)


class KrittTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_full: str = Field(min_length=1)
    source_root: str = Field(min_length=1)
    source_manifest: str = Field(min_length=1)
    scan_id: int | None = Field(default=None, gt=0)
    commit: str | None = None
    references: list[str] = Field(default_factory=list)


class KrittConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsn_env: str = Field(default="CAIRN_KRITT_READONLY_DSN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    profile: Literal["kritt_v1"] = "kritt_v1"
    targets: dict[str, KrittTargetConfig] = Field(min_length=1)
    max_rows: int = Field(default=20000, gt=0)
    max_bytes: int = Field(default=33554432, gt=0)
    statement_timeout_ms: int = Field(default=10000, gt=0)
    response_rows: int = Field(default=20, gt=0, le=100)
    response_bytes: int = Field(default=65536, ge=4096, le=1048576)
    max_queries: int = Field(default=12, gt=0)
    max_agent_calls: int = Field(default=8, gt=0)
    probe_seconds: int = Field(default=600, gt=0)


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: ControllerRoles
    research_prompt_group: str | None = None
    poll_interval: int = Field(default=3, gt=0)
    max_goal_revisions: int = Field(default=2, ge=0)
    timeouts: ControllerTimeouts = Field(default_factory=ControllerTimeouts)
    review: ControllerReviewConfig = Field(default_factory=ControllerReviewConfig)
    budget: ControllerBudgetConfig = Field(default_factory=ControllerBudgetConfig)
    kritt: KrittConfig | None = None


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig | None = None
    local: LocalConfig | None = None
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig] = Field(default_factory=list)
    controller: ControllerConfig | None = None
    role_pools: RolePools | None = None

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("role_pools") is not None:
            pools = TypeAdapter(RolePools).validate_python(data["role_pools"])
            runtime = data.get("runtime", {})
            runtime = runtime.model_dump() if isinstance(runtime, BaseModel) else dict(runtime)
            if runtime.get("execution", "container") != "local":
                raise ValueError("role_pools requires runtime.execution=local")
            required = {"explore", "reason"}
            if data.get("controller") is not None:
                required.update({"preflight", "seed_review", "overseer", "closing", "final_review"})
            missing = sorted(required - pools.keys())
            if missing:
                raise ValueError(f"role_pools missing required roles: {', '.join(missing)}")
            merged = dict(data)
            merged["workers"] = [
                {"name": f"pool-{role}-{harness}", "type": harness, "task_types": [role],
                 "max_running": count, "priority": 0, "env": {}}
                for role, members in pools.items() for harness, count in members.items()
            ]
            # Reason and Bootstrap each make one project-wide decision at a time.
            capacity = sum(pools["explore"].values()) + 1
            runtime["max_project_workers"] = capacity
            runtime["max_workers"] = capacity * runtime.get("max_running_projects", 1)
            merged["runtime"] = runtime
            if data.get("controller") is not None:
                controller = data["controller"]
                controller = controller.model_dump() if isinstance(controller, BaseModel) else dict(controller)
                build_role = "build" if "build" in pools else "closing"
                controller["roles"] = {
                    "builder": f"pool-{build_role}-{next(iter(pools[build_role]))}",
                    "reviewer": f"pool-preflight-{next(iter(pools['preflight']))}",
                }
                merged["controller"] = controller
            return merged
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        if self.role_pools is not None:
            from cairn.dispatcher.workers.registry import get_driver

            for worker in self.workers:
                get_driver(worker.type, self.runtime.execution)
        if self.controller is not None:
            from cairn.dispatcher.workers.registry import get_driver

            by_name = {worker.name: worker for worker in self.workers}
            for role, name in self.controller.roles.model_dump().items():
                if name not in by_name:
                    raise ValueError(f"controller role {role} references unknown worker {name!r}")
                get_driver(by_name[name].type, self.runtime.execution)
        return self

    def pool_workers(self, role: str) -> list[WorkerConfig]:
        if self.role_pools is None:
            return []
        return [worker for worker in self.workers if worker.task_types == [role]]

    def pool_capacity(self, role: str) -> int:
        return sum(worker.max_running for worker in self.pool_workers(role))

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "DispatchConfig":
        if self.runtime.execution == "container":
            if self.container is None:
                raise ValueError("container config is required when runtime.execution is container")
            for worker in self.workers:
                required = WORKER_ENV_KEYS[worker.type]
                missing = [key for key in required if not worker.env.get(key)]
                if missing:
                    raise ValueError(f"worker {worker.name} missing env keys: {', '.join(missing)}")
        else:  # local: workers reuse the host CLI config, so no LLM env keys are required
            if self.local is None:
                self.local = LocalConfig()
            if self.controller is not None and self.controller.kritt is not None:
                secret_env = self.controller.kritt.dsn_env
                self.local.excluded_env = list(dict.fromkeys([*self.local.excluded_env, secret_env]))
        return self

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        if config.controller is not None and config.controller.kritt is not None:
            for target in config.controller.kritt.targets.values():
                for field in ("source_root", "source_manifest"):
                    value = Path(getattr(target, field)).expanduser()
                    if not value.is_absolute():
                        value = path.resolve().parent / value
                    setattr(target, field, str(value.resolve()))
                target.references = [str((value if value.is_absolute() else path.resolve().parent / value).resolve())
                                     for value in (Path(item).expanduser() for item in target.references)]
        validate_prompt_resources(config.runtime.prompt_group)
        if config.controller is not None and config.controller.research_prompt_group is not None:
            validate_prompt_resources(config.controller.research_prompt_group)
        return config


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("cairn.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = PROMPT_REQUIRED_TOKENS_BY_GROUP.get(prompt_group, DEFAULT_PROMPT_REQUIRED_TOKENS)
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")


def resolve_mock_behavior(worker_name: str, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    unknown = sorted(key for key in env if key.startswith("MOCK_") and key not in MOCK_ALLOWED_ENV_KEYS)
    if unknown:
        raise ValueError(f"worker {worker_name} has unsupported mock env keys: {', '.join(unknown)}")

    behavior: dict[str, dict[str, Any]] = {}
    for phase, allowed_outcomes in MOCK_ALLOWED_OUTCOMES.items():
        prefix = _mock_env_prefix(phase)
        payload = _parse_mock_phase_payload(worker_name, env, prefix, MOCK_DEFAULT_BEHAVIOR[phase])
        min_delay, max_delay = _parse_mock_delay_range(worker_name, prefix, payload.get("delay"))
        if max_delay < min_delay:
            raise ValueError(f"worker {worker_name} {prefix}.delay[1] must be greater than or equal to delay[0]")
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, dict):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes must be an object")
        unknown_outcomes = sorted(set(raw_outcomes) - allowed_outcomes)
        if unknown_outcomes:
            raise ValueError(f"worker {worker_name} {prefix}.outcomes has unsupported keys: {', '.join(unknown_outcomes)}")
        outcomes: dict[str, float] = {}
        total = Decimal("0")
        for outcome in sorted(allowed_outcomes):
            weight = _parse_mock_probability(
                worker_name,
                prefix,
                raw_outcomes,
                outcome,
            )
            outcomes[outcome] = float(weight)
            total += weight
        if total != Decimal("1"):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes probabilities must sum to 1.0, got {total}")
        behavior[phase] = {
            "delay": {"min": min_delay, "max": max_delay},
            "outcomes": outcomes,
        }
        rules = payload.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ValueError(f"worker {worker_name} {prefix}.rules must be an array")
            normalized_rules: list[dict[str, Any]] = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"worker {worker_name} {prefix}.rules[{index}] must be an object")
                force = rule.get("force")
                if not isinstance(force, str) or force not in allowed_outcomes:
                    raise ValueError(
                        f"worker {worker_name} {prefix}.rules[{index}].force must be one of: {', '.join(sorted(allowed_outcomes))}"
                    )
                entry: dict[str, Any] = {"force": force}
                if "fact_ids_gte" in rule:
                    value = rule["fact_ids_gte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_gte must be a non-negative integer")
                    entry["fact_ids_gte"] = value
                if "fact_ids_lte" in rule:
                    value = rule["fact_ids_lte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_lte must be a non-negative integer")
                    entry["fact_ids_lte"] = value
                if "open_intents_empty" in rule:
                    value = rule["open_intents_empty"]
                    if not isinstance(value, bool):
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].open_intents_empty must be boolean")
                    entry["open_intents_empty"] = value
                normalized_rules.append(entry)
            behavior[phase]["rules"] = normalized_rules
    return behavior


def _mock_env_prefix(phase: str) -> str:
    return f"MOCK_{phase.upper()}"


def _parse_mock_phase_payload(worker_name: str, env: dict[str, str], key: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = env.get(key)
    if raw is None:
        return json.loads(json.dumps(default))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker {worker_name} {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"worker {worker_name} {key} must be a JSON object")
    return value


def _parse_mock_delay_range(worker_name: str, key: str, value: Any) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"worker {worker_name} {key}.delay must be a two-element number array")
    min_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[0]", value[0])
    max_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[1]", value[1])
    return min_delay, max_delay


def _coerce_mock_seconds(worker_name: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"worker {worker_name} {key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"worker {worker_name} {key} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"worker {worker_name} {key} must be non-negative")
    return parsed


def _parse_mock_probability(worker_name: str, phase_key: str, outcomes: dict[str, Any], outcome: str) -> Decimal:
    raw = outcomes.get(outcome, MOCK_DEFAULT_BEHAVIOR[phase_key.removeprefix("MOCK_").lower()]["outcomes"][outcome])
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be a decimal probability") from exc
    if value < 0 or value > 1:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be between 0 and 1")
    return value
