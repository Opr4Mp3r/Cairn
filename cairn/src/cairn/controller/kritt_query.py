"""Read one saved Kritt view through the harness's command tool."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from cairn.audit_evidence import json_digest
from cairn.controller.kritt import KrittView
from cairn.controller.kritt_tools import GetAction, KrittTools, PROFILE_VERSION, ToolAction


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_query(context_path: Path, request: dict) -> dict:
    context = json.loads(context_path.read_text(encoding="utf-8"))
    query_id = f"q-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    folder = Path(context["queries_dir"]) / query_id
    folder.mkdir(parents=True)
    write_json(folder / "request.json", request)
    tools = None
    try:
        view = KrittView.load(context["view_path"], max_rows=context["max_rows"], max_bytes=context["max_bytes"])
        if view.digest != context["view_digest"]:
            raise ValueError("query context does not match its fixed Kritt view")
        tools = KrittTools(view, max_rows=context["response_rows"], max_bytes=context["response_bytes"])
        action = dict(request)
        if action.get("action") == "search":
            # route is a legacy receipt label; it does not affect retrieval.
            action.setdefault("route", "candidates")
        if action.get("action") == "get":
            action.pop("cursor", None)  # A get command always delivers the whole branch.
        action = ToolAction.model_validate(action).root
        result = tools.execute(action)
        while isinstance(action, GetAction) and result.receipt.next_cursor is not None:
            action = action.model_copy(update={"cursor": result.receipt.next_cursor})
            result = tools.execute(action)

        tools.save_receipts(folder / "receipts.json")
        output = {"status": "success", "query_id": query_id, "action": result.action,
                  "view_digest": view.digest, "scan_id": view.scan["id"],
                  "commit_sha": view.scan["commit_sha"], "receipts_path": str(folder / "receipts.json")}
        if isinstance(action, GetAction):
            materials = [{"ref": ref.model_dump(mode="json"), "content": tools.resolve(ref)} for ref in tools.ledger]
            write_json(folder / "materials.json", materials)
            output.update(ref=request["ref"], material_count=len(materials), materials_path=str(folder / "materials.json"))
        else:
            summary = dict(result.summary)
            summary.pop("routes", None)
            summary.pop("route", None)
            output.update(summary=summary, records=[row.model_dump(mode="json") for row in result.records],
                          next_cursor=result.receipt.next_cursor)
        write_json(folder / "result.json", output)
        return output
    except Exception as exc:
        if tools is not None:
            tools.save_receipts(folder / "receipts.json")
        write_json(folder / "result.json", {"status": "error", "query_id": query_id,
                                            "error": f"{type(exc).__name__}: {exc}"})
        raise


def collect_queries(context_path: Path) -> dict:
    """Merge independently saved calls; only successful gets contribute materials."""
    context = json.loads(context_path.read_text(encoding="utf-8"))
    receipts, materials, queries = [], {}, []
    for folder in sorted(Path(context["queries_dir"]).glob("q-*")):
        result_path, receipt_path = folder / "result.json", folder / "receipts.json"
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        queries.append(result)
        if receipt_path.exists():
            saved = json.loads(receipt_path.read_text(encoding="utf-8"))
            if saved["view_digest"] != context["view_digest"]:
                raise ValueError("query receipts belong to a different Kritt view")
            receipts.extend(saved["receipts"])
        if result["status"] != "success" or result["action"] != "get":
            continue
        for material in json.loads((folder / "materials.json").read_text(encoding="utf-8")):
            materials[json_digest(material["ref"])] = material
    run = context_path.parent
    write_json(run / "query-receipts.json", {
        "view_digest": context["view_digest"], "profile_version": PROFILE_VERSION,
        "ledger": [value["ref"] for value in materials.values()], "receipts": receipts, "queries": queries,
    })
    write_json(run / "selected-materials.json", list(materials.values()))
    return {"queries": len(queries), "query_pages": len(receipts)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a fixed Kritt view. Send describe/search/get JSON on stdin.")
    parser.add_argument("--context", type=Path, required=True, help="Controller-generated kritt-context.json")
    args = parser.parse_args()
    try:
        output = run_query(args.context, json.load(sys.stdin))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
