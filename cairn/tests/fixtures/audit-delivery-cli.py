"""CP6 的固定判断替身；真实 CLI 演示另行记录。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if "--help" in sys.argv:
    print("CP6 fake Claude")
    raise SystemExit(0)

prompt = sys.argv[-1]
decoder = json.JSONDecoder()
context = {}
for index, char in enumerate(prompt):
    if char == "{":
        try:
            value, _ = decoder.raw_decode(prompt[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and "repo_path" in value and ("output_schema" in value or "verified_fact_ids" in value):
            context = value
title = context.get("output_schema", {}).get("title")
phase = {"AuditPlan": "build", "PreflightResult": "preflight", "SeedReviewResult": "seed_review",
         "NodeAssessment": "overseer", "FinalAssessment": "final_review", "ClosingResult": "closing"}.get(title)
if phase is None:
    phase = "explore" if "Current Intent" in prompt else "reason"
call = {"pid": os.getpid(), "phase": phase}
if os.environ.get("CP6_BLOCK") == phase:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    call["child_pid"] = child.pid
with open(os.environ["CP6_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(call) + "\n")
if "child_pid" in call:
    time.sleep(120)
if os.environ.get("CP6_FAIL") == phase:
    raise SystemExit(7)

budget = os.environ.get("CP6_OUTCOME") == "budget"
evidence = {"path": "app.py", "start_line": 1, "end_line": len((Path(context["repo_path"]) / "app.py").read_text().splitlines())}
if phase == "build":
    result = {"title": "CP6 fixed-output audit", "origin": "local source fixture", "goal": context["user_goal"],
              "policy": {"effect": "trace public source", "deployment": "local source fixture", "cvss": {},
                         "attacker_capabilities": ["anonymous input"], "scope": ["app.py"],
                         "evidence_requirements": ["source reference"],
                         "threat_model": [{"source": "app.py", "kind": "fixture"}]},
              "seeds": [], "hints": [], "intents": [{"from": ["origin"], "description": "read public fixture branch"}]}
elif phase == "preflight":
    result = {"approved": True, "issues": []}
elif phase == "seed_review":
    result = {"reviews": []}
elif phase == "overseer":
    target = context["target"]
    data = target.get("audit")
    if data is not None:
        data = {**data, "review_status": "unverified" if budget else "verified", "review_notes": "controlled source judgement"}
    result = {"decision": "needs_evidence" if budget else "approved", "reason": "controlled fixture judgement", "fact_audit": data, "hint": None}
elif phase == "closing":
    facts = [fact["id"] for fact in context["graph"]["facts"] if fact.get("audit", {}).get("review_status") == "verified"]
    result = {"kind": "no_candidate" if budget or not facts else "candidate", "from": [] if budget or not facts else facts[:1],
              "description": "only existing source evidence"}
elif phase == "final_review":
    result = {"decision": "approved", "reason": "controlled source and fixed-policy judgement", "proof_ids": context["request"]["from"],
              "checks": dict.fromkeys(["effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence"], True), "cvss_vector": None}
elif phase == "explore":
    assert "PUBLIC_DIAGNOSTIC" in (Path(context["repo_path"]) / "app.py").read_text()
    result = {"accepted": True, "data": {"description": "public branch found in fixed fixture", "audit": {
        "evidence": [evidence], "conditions": [], "premise_ids": [], "review_status": "unverified",
        "validation_level": "source_analysis", "review_notes": "fixed-output test"}}}
else:
    facts = context.get("verified_fact_ids", [])
    if facts and not budget:
        result = {"accepted": True, "data": {"complete": {"from": facts[:1], "description": "source goal supported"}}}
    else:
        result = {"accepted": True, "data": {"intents": [{"from": ["origin"], "description": "verify public branch"}]}}
print(json.dumps(result, ensure_ascii=False))
