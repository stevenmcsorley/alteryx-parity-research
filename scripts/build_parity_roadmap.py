#!/usr/bin/env python3
"""Render the full catalogue roadmap from the evidence-derived status and contracts."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).replace("|", "\\|").strip() or "—"


def _contract_path(catalog_id: str, category: str, name: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return ROOT / "tool-contracts" / category.lower().replace(" ", "-") / f"{slug}.contract.json"


def render() -> str:
    catalog = json.loads((ROOT / "data/catalog.json").read_text(encoding="utf-8"))
    ledger_doc = json.loads((ROOT / "data/tool-status.json").read_text(encoding="utf-8"))
    ledger = {row["id"]: row for row in ledger_doc["tools"]}
    from flowengine.plugin import default_registry
    registry = default_registry()
    tools = catalog["tools"]
    lines = [
        "# Alteryx parity roadmap",
        "",
        "> **Generated source of truth.** Rebuild with `python scripts/build_parity_roadmap.py`; do not hand-edit rows. "
        "Catalogue identity comes from `data/catalog.json`; implementation and E1/E2 test status come from "
        "`data/tool-status.json`, generated from registered kernels, executed test attribution and conformance evidence. "
        "Contracts provide observed anchors, declared configuration evidence, dependencies and unresolved behaviour. "
        "Unknown means unknown; it is never inferred as supported.",
        "",
        f"Catalogue snapshot: {catalog.get('metadata', {}).get('target_version', 'pinned repository snapshot')} "
        "(do not sync rolling changes without review). Behavioural reference: Designer 2026.2.0.8, E1 and E2. "
        f"Entries: **{len(tools)}**. Last evidence-ledger test run: "
        f"{ledger_doc['test_run']['tests_run']} run, {ledger_doc['test_run']['tests_failed']} failed. "
        "See `docs/TOOL_STATUS_RULES.md` for status definitions.",
        "",
        "## Per-entry work queue",
        "",
        "The next action is deliberately entry-specific and evidence-led. A kernel does not imply full option "
        "coverage; `partial` means recorded differential cases passed, not full compatibility.",
        "",
        "| Catalogue ID | Category / tool | Implementation | Supported options and anchors | Missing capabilities / evidence | Dependencies | Reference coverage | Known behavioural differences | Blocker | Next action |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    if len(ledger) != len(tools) or set(ledger) != {tool["id"] for tool in tools}:
        raise ValueError("Catalogue and evidence ledger do not contain the same IDs")
    for tool in tools:
        cid = tool["id"]
        row = ledger[cid]
        contract_file = _contract_path(cid, tool["category"], tool["name"])
        contract = json.loads(contract_file.read_text(encoding="utf-8")) if contract_file.exists() else {}
        schema = contract.get("configuration_schema", {})
        tool_impl = registry.get(row["kernel"]) if row.get("kernel") else None
        if tool_impl:
            config_schema = tool_impl.manifest.config_schema
            properties = config_schema.get("properties", {})
            options = ", ".join(f"{key}={json.dumps(value.get('enum'), ensure_ascii=False)}"
                                 if "enum" in value else key for key, value in properties.items())
        else:
            options = ", ".join(schema.get("reference_config_elements", [])) if isinstance(schema, dict) else ""
        anchors = []
        if tool_impl:
            anchors.extend(f"input:{a.name}" for a in tool_impl.manifest.inputs)
            anchors.extend(f"output:{a.name}" for a in tool_impl.manifest.outputs)
        else:
            for direction in ("inputs", "outputs"):
                anchors.extend(f"{direction[:-1]}:{a['name']}" for a in contract.get(direction, []))
        supported = []
        if row.get("kernel"):
            supported.append(f"kernel `{row['kernel']}`; options `{options or 'not enumerated in contract'}`")
        else:
            supported.append("no kernel")
        supported.append("anchors " + (", ".join(anchors) if anchors else "not known"))
        unresolved = contract.get("unresolved", [])
        missing = "; ".join(unresolved) if unresolved else (
            "full configuration matrix not verified" if row["verification_status"] != "verified" else "none recorded")
        deps = list(contract.get("external_dependencies", []))
        if tool_impl:
            deps.extend(tool_impl.manifest.dependencies)
        deps = sorted(set(deps))
        if not deps:
            deps = ["none recorded"]
        refs = (f"E1 {row.get('conformance_passed', 0)}/{row.get('conformance_cases', 0)}; "
                f"E2 {row.get('e2_passed', 0)}/{row.get('e2_cases', 0)}; "
                f"status {row['verification_status']}; structural observation "
                f"{'yes' if row.get('reference_observed') else 'no'}")
        failures = [case.get("case_id", "unnamed") for case in contract.get("cases", [])
                    if case.get("status") == "failed"]
        differences = "; ".join(failures) if failures else "no failing recorded case; untested differences unknown"
        blocker = ("external prerequisite: " + ", ".join(deps)
                   if row.get("external_prerequisites_blocking") else "none recorded")
        if row.get("implementation_status") == "not-started":
            action = "specify behaviour from contract and authorised evidence; implement and add deterministic tests"
        elif row.get("implementation_status") == "in-progress":
            action = "resolve failing/missing kernel tests, then add reference differential cases"
        elif row.get("verification_status") == "failed":
            action = "investigate the recorded reference difference; preserve it until evidence supports a fix"
        elif row.get("verification_status") == "partial":
            action = "expand configuration and edge-case differential matrix"
        else:
            action = "add first differential cases against pinned E1/E2 references"
        cells = [cid, f"{tool['category']} / {tool['name']}",
                 f"{row['implementation_status']} ({row.get('independent_tests', 0)} tests)",
                 "; ".join(supported), missing, ", ".join(deps), refs, differences, blocker, action]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    lines.extend([
        "",
        "## Priority sequence",
        "",
        "1. Standard and batch macro runtime checkpoints: nested typed workflows, imported `.yxmc` boundaries, "
        "ordered per-control-record runs, declared child-config Actions, strict output schema matching and zero-row "
        "schema inference now execute. **Next item: map real imported Action/Control Parameter node and anchor "
        "configuration into this runtime, then compare against authorized reference runs.** Then implement iterative "
        "loop state, termination, limits and cancellation.",
        "2. Interface tool semantics and analytic application execution in the existing desktop.",
        "3. Remaining core, Developer and Documentation tools, selected by dependencies and evidence in the table.",
        "4. Spatial, licensed-data, external-service and other blocked tools when their stated prerequisites are available.",
        "",
        "This sequence orders work; it does not remove any catalogue row from scope. Database provider or optimizer "
        "expansion is outside this roadmap unless a demonstrated parity defect requires it.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the generated roadmap is stale")
    args = parser.parse_args()
    path = ROOT / "docs/ALTERYX_PARITY_ROADMAP.md"
    rendered = render()
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            print("Roadmap is stale; run python scripts/build_parity_roadmap.py")
            return 1
        print("Roadmap matches the catalogue and evidence ledger")
        return 0
    path.write_text(rendered, encoding="utf-8")
    print(f"Wrote {path} ({len(json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))['tools'])} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
