from __future__ import annotations

import json
from contextlib import closing
import shutil
from pathlib import Path
import time
import uuid
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cairn.controller.app import call_agent
from cairn.controller.pool import run_pooled
from cairn.audit_evidence import json_digest, repository_baseline, source_evidence, assert_repository_unchanged
from cairn.audit_inputs import agent_view, READING_GUIDANCE
from cairn.controller.kritt_query import prepare_research_query, write_json
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.server.models import (
    AuditBudget, AuditFactData, AuditInitialIntent, AuditRecordRequest, AuditSeed,
    CreateAuditRequest, CreateHintInline, ProjectDetail,
)


class AuditPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: str = Field(min_length=1)
    deployment: str = Field(min_length=1)
    attacker_capabilities: list[str] = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    cvss: dict[str, JsonValue]
    evidence_requirements: list[str] = Field(min_length=1)
    threat_model: list[dict[str, JsonValue]] = Field(min_length=1)


class AuditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    policy: AuditPolicy
    seeds: list[AuditSeed] = Field(default_factory=list)
    intents: list[AuditInitialIntent] = Field(min_length=1)
    hints: list[CreateHintInline] = Field(default_factory=list)


class PreflightNeedsRevision(ValueError):
    def __init__(self, issues: list[str], run_dir: Path):
        self.issues = issues
        self.receipts = str(run_dir)
        super().__init__(f"goal preflight requires revision: {issues}; receipts: {run_dir}")


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    issues: list[str]

    @model_validator(mode="after")
    def validate_decision(self):
        if self.approved == bool(self.issues) or any(not issue.strip() for issue in self.issues):
            raise ValueError("approved requires no issues; rejection requires concrete issues")
        return self


class SeedAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    audit: AuditFactData


class SeedReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[SeedAssessment]


def apply_seed_reviews(plan: AuditPlan, result: SeedReviewResult, repo: Path, baseline: dict | None = None) -> None:
    by_key = {entry.key: entry.audit for entry in result.reviews}
    if len(by_key) != len(result.reviews) or set(by_key) != {seed.key for seed in plan.seeds}:
        raise ValueError("seed review must cover every seed exactly once")
    baseline = baseline if baseline is not None else repository_baseline(repo, repo)
    files = baseline.get("files", {})
    for seed in plan.seeds:
        data = by_key[seed.key].model_copy(deep=True)
        try:
            data.evidence = source_evidence(repo, data.evidence, baseline)
            if data.review_status != "unverified" and not data.evidence:
                raise ValueError("confirmed or refuted seed requires source evidence")
            if data.review_status != "unverified" and any(
                not isinstance(files, dict) or files.get(reference.path) != reference.sha256 for reference in data.evidence
            ):
                raise ValueError("seed source evidence changed or is not snapshotted in the fixed baseline")
        except (OSError, ValueError, RuntimeError) as exc:
            data.review_status = "unverified"
            data.review_notes = f"{data.review_notes or ''}\n引用复核未通过：{exc}".strip()
        seed.audit = data
    # 已确认的依赖主张不能暗中继承尚未确认的前提。
    for _ in plan.seeds:
        changed = False
        states = {seed.key: seed.audit.review_status for seed in plan.seeds}
        for seed in plan.seeds:
            if seed.audit.review_status == "verified" and any(ref != "origin" and states.get(ref) != "verified" for ref in seed.audit.premise_ids):
                seed.audit.review_status = "unverified"
                seed.audit.review_notes = f"{seed.audit.review_notes or ''}\n必要前提尚未独立确认".strip()
                changed = True
        if not changed:
            break


def _require_ok(response: ApiResult, action: str):
    if not response.ok:
        raise ValueError(f"{action} failed ({response.status_code}): {response.text}")
    return response.data


def _stage(config, backend, repo, workspace_root, baseline, run_dir, receipts, cancellation,
           phase: str, role: str, model: type[BaseModel], context: dict, *, session=None, worker=None):
    instruction = resources.files("cairn.controller").joinpath(f"prompts/{phase}.md").read_text(encoding="utf-8")
    instruction += "\n" + READING_GUIDANCE
    payload = {**agent_view(context), "output_schema": model.model_json_schema(by_alias=True)}
    if phase == "seed_review":
        all_keys = [seed["key"] for seed in context["plan"]["seeds"]]
        keys = context.get("review_keys", all_keys)
        allowed = ["origin", *all_keys]
        payload["valid_fact_references"] = allowed
        payload["origin_fact_id"] = "origin"
        definitions = payload["output_schema"]["$defs"]
        definitions["AuditFactData"]["properties"]["premise_ids"]["items"]["enum"] = allowed
        if keys:
            definitions["SeedAssessment"]["properties"]["key"]["enum"] = keys
    deadline = time.monotonic() + getattr(config.controller.timeouts, phase)
    for attempt in range(2):
        name = phase if attempt == 0 else f"{phase}_format_repair"
        remaining = int(deadline - time.monotonic()) - 5
        if remaining <= 0:
            raise ValueError(f"{phase} deadline exhausted; receipts: {run_dir}")
        folder = run_dir / f"{len(receipts) + 1:02d}-{name}"
        folder.mkdir()
        prompt = instruction + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        (folder / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            result = call_agent(config, backend, role=role, prompt=prompt, result_model=model,
                                workspace=run_dir, timeout_seconds=remaining, phase=name, cancellation=cancellation, session=session,
                                **({"worker": worker} if worker is not None else {}))
        except (OSError, ValueError) as exc:
            (folder / "result.json").write_text(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), encoding="utf-8")
            raise
        if phase == "seed_review" and result.status == "success":
            assessments = result.data["reviews"]
            if len(assessments) != len(keys) or {entry["key"] for entry in assessments} != set(keys) or any(
                not set(entry["audit"]["premise_ids"]) <= set(allowed) for entry in assessments
            ):
                result.status = "invalid_output"
                result.error = f"reviews must cover each seed once; premise_ids must be IDs from {allowed}, not plan.origin description"
                result.data = None
        artifact = folder / "result.json"
        artifact.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        receipts.append((name, payload, result, artifact))
        assert_repository_unchanged(repo, baseline, workspace_root, cancellation=cancellation)
        session = result.session
        if result.status == "success":
            return model.model_validate(result.data)
        if result.status != "invalid_output" or attempt == 1:
            raise ValueError(f"{name}: {result.error or result.status}; receipt: {artifact}")
        instruction = "只修复以下已有输出以符合 schema，不调用工具、不重新研究、不添加证据或改变原语义。仅返回一个原始 JSON 对象。"
        payload = {"previous_response": result.response, "validation_error": result.error,
                   "output_schema": payload["output_schema"]}


def _review_seeds(config, backend, repo, workspace_root, baseline, run_dir, receipts, cancellation, plan, context):
    payload = {**context, "plan": plan.model_dump(mode="json", by_alias=True)}
    count = min(len(plan.seeds), config.pool_capacity("seed_review")) if config.role_pools else 1
    if count <= 1:
        return _stage(config, backend, repo, workspace_root, baseline, run_dir, receipts,
                      cancellation, "seed_review", "reviewer", SeedReviewResult, payload)
    keys = [seed.key for seed in plan.seeds]
    batches = [(index, keys[index::count]) for index in range(count)]
    batch_receipts = [[] for _ in batches]

    def invoke(batch, worker, child):
        index, review_keys = batch
        folder = run_dir / f"seed-batch-{index + 1:03d}"
        folder.mkdir()
        return _stage(config, backend, repo, workspace_root, baseline, folder, batch_receipts[index],
                      child, "seed_review", "reviewer", SeedReviewResult,
                      {**payload, "review_keys": review_keys}, worker=worker)

    reviews = []
    try:
        with closing(run_pooled(config, "seed_review", batches, invoke, cancellation)) as results:
            for _, reviewed in results:
                reviews.extend(reviewed.reviews)
    finally:
        for batch in batch_receipts:
            receipts.extend(batch)
    return SeedReviewResult(reviews=reviews)


def prepare_audit(config: DispatchConfig, client: CairnClient, backend: LocalBackend, *,
                  plan: AuditPlan, context: dict, run_dir: Path, revise=None, receipts=None,
                  cancellation: TaskCancellation | None = None) -> dict:
    """Review and activate an existing plan, shared by source and Kritt Builders."""
    if config.controller is None or config.runtime.execution != "local" or not config.controller.research_prompt_group:
        raise ValueError("audit requires local execution and controller.research_prompt_group")
    repo = Path(context["repo_path"]).resolve(strict=True)
    workspace_root = Path(context["workspace_root"]).resolve()
    baseline = context["baseline"]
    run_dir.mkdir(parents=True, exist_ok=True)
    receipts = [] if receipts is None else receipts
    cancellation = cancellation or TaskCancellation()
    client.get_settings()
    assert_repository_unchanged(repo, baseline, workspace_root, cancellation=cancellation)

    def stage(phase, role, model, payload):
        return _stage(config, backend, repo, workspace_root, baseline, run_dir, receipts,
                      cancellation, phase, role, model, payload)

    for revision in range(config.controller.max_goal_revisions + 1):
        for seed in plan.seeds:
            seed.audit.review_status = "unverified"
        verdict = stage("preflight", "reviewer", PreflightResult,
                        {**context, "plan": plan.model_dump(mode="json", by_alias=True)})
        if verdict.approved:
            break
        if revise is None:
            raise PreflightNeedsRevision(verdict.issues, run_dir)
        if revision == config.controller.max_goal_revisions:
            raise ValueError(f"goal preflight failed after {revision + 1} attempts: {verdict.issues}; receipts: {run_dir}")
        plan = revise(plan, verdict.issues)
    if plan.seeds:
        reviewed = _review_seeds(config, backend, repo, workspace_root, baseline, run_dir, receipts,
                                 cancellation, plan, context)
        apply_seed_reviews(plan, reviewed, repo, baseline)
    existing_sources = {"origin", *(seed.key for seed in plan.seeds)}
    for seed in plan.seeds:
        if seed.audit.review_status == "unverified":
            plan.intents.append(AuditInitialIntent(**{"from": [seed.key], "description": f"独立验证前提：{seed.description}；检查权限拦截和必要条件，不预设成立。"}))
        if not set(seed.audit.premise_ids) <= existing_sources:
            raise ValueError(f"unknown seed premise: {seed.key}")
    plan_path = Path(context.get("plan_path", run_dir / "plan.json"))
    write_json(plan_path, plan.model_dump(mode="json", by_alias=True))
    request = CreateAuditRequest(
        **plan.model_dump(mode="json", by_alias=True), user_goal=context["user_goal"],
        repo_path=str(repo), workspace_root=str(workspace_root), baseline=baseline,
        budget=AuditBudget(**config.controller.budget.model_dump()),
    )
    if cancellation.is_cancelled:
        raise ValueError(cancellation.reason or "cancelled")
    project = ProjectDetail.model_validate(_require_ok(client.create_audit(request), "create audit"))
    metadata = project.project.audit
    assert metadata is not None
    summary = {"status": "preparing", "project_id": project.project.id,
               "seed_ids": metadata.seed_ids, "intent_ids": [i.id for i in project.intents if i.to is None],
               "controller_run": str(run_dir), "baseline": baseline["digest"]}
    locator = Path(context.get("project_path", run_dir / "project.json"))

    def save_locator():
        if "project_path" in context:
            write_json(locator, summary)
        elif summary["status"] == "active":
            locator.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    save_locator()
    workspace = Path(backend.ensure_running(project.project.id))
    if workspace.resolve() != workspace_root / project.project.id:
        raise ValueError("backend workspace does not match audit workspace")
    (workspace / "repo").symlink_to(repo, target_is_directory=True)
    material_paths = {}
    for name, original in context.get("materials", {}).items():
        destination = workspace / name
        shutil.copyfile(original, destination)
        material_paths[name] = str(destination)
    write_json(workspace / "input.json", {
        "project_id": project.project.id, "repo_path": metadata.repo_path,
        "policy_digest": metadata.policy_digest, "controller_run": str(run_dir), "materials": material_paths,
        "kritt_query": prepare_research_query(workspace, context.get("kritt_query_context")),
    })
    summary["workspace"] = str(workspace)
    save_locator()
    for index, (phase, payload, result, artifact) in enumerate(receipts):
        success = result.status == "success"
        record = AuditRecordRequest(
            record_key=f"{run_dir.name}:{index}:{phase}", object_type="project", object_id=project.project.id,
            input_digest=json_digest(payload), phase=phase, session=result.session, attempt_status="success" if success else "error",
            error=None if success else result.error or result.status,
            decision=("approved" if result.data["approved"] else "rejected") if success and phase.startswith("preflight") else None,
            result=result.data, artifacts=[str(artifact)],
        )
        _require_ok(client.record_audit_review(project.project.id, record), "record controller call")
    assert_repository_unchanged(repo, baseline, workspace_root, cancellation=cancellation)
    try:
        _require_ok(client.start_audit(project.project.id), "activate audit")
        if cancellation.is_cancelled:
            raise ValueError(cancellation.reason or "cancelled after activation")
        summary.update(status="active", note="Audit activated; fixed policy, oversight and budget use the prepared source.")
        save_locator()
        if cancellation.is_cancelled:
            raise ValueError(cancellation.reason or "cancelled during handover")
        return summary
    except BaseException as exc:
        try:
            _require_ok(client.set_project_status(project.project.id, "stopped"), "stop after activation failure")
        except BaseException as stop_error:
            raise RuntimeError(f"project {project.project.id}: activation or handover failed ({exc}); stop failed: {stop_error}; state uncertain") from stop_error
        raise ValueError(f"project {project.project.id} stopped after activation or handover failure: {exc}") from exc


def create_local_audit(
    config: DispatchConfig, client: CairnClient, backend: LocalBackend, *, repo: Path,
    user_goal: str, workspace_root: Path, references: tuple[Path, ...] = (),
    input_seeds: Path | None = None, cancellation: TaskCancellation | None = None,
) -> dict:
    if config.controller is None or config.runtime.execution != "local" or not config.controller.research_prompt_group:
        raise ValueError("audit requires local execution and controller.research_prompt_group")
    if not user_goal.strip():
        raise ValueError("audit goal must not be empty")
    repo, workspace_root = repo.expanduser().resolve(), workspace_root.expanduser().resolve()
    if not repo.is_dir() or workspace_root == repo:
        raise ValueError("repository must exist and differ from workspace root")
    for reference in references:
        reference.read_text(encoding="utf-8")
    supplied = [] if input_seeds is None else [AuditSeed.model_validate(entry) for entry in json.loads(input_seeds.read_text(encoding="utf-8"))]
    for seed in supplied:
        seed.audit.review_status = "unverified"
    client.get_settings()
    baseline = repository_baseline(repo, workspace_root)
    run_dir = workspace_root / "controller" / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    cancellation = cancellation or TaskCancellation()
    receipts = []
    context = {"repo_path": str(repo), "workspace_root": str(workspace_root), "user_goal": user_goal,
               "reference_paths": [str(path.resolve()) for path in references], "baseline": baseline,
               "input_seeds": [seed.model_dump(mode="json", by_alias=True) for seed in supplied]}
    builder_session = None

    def build(current=None, issues=None):
        nonlocal builder_session
        payload = {**context, "preflight_issues": issues or []}
        if current is not None:
            payload["plan"] = current.model_dump(mode="json", by_alias=True)
        value = _stage(config, backend, repo, workspace_root, baseline, run_dir, receipts, cancellation,
                       "build", "builder", AuditPlan, payload, session=builder_session)
        builder_session = receipts[-1][2].session
        return value

    return prepare_audit(config, client, backend, plan=build(), context=context, run_dir=run_dir,
                         revise=build, receipts=receipts, cancellation=cancellation)
