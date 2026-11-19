from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from agent.__main__ import build_client, build_pipeline
from agent.config import load_settings
from agent.runlock import STATE_DIR
from agent.schema import Decision, Ticket

# Scores the agent against Nordavo's 10 labelled examples (GET /examples).
# The labels are theirs and the comparison is plain equality: no model grades anything.
# It runs the same pipeline as the real agent, with nothing sent to the helpdesk.

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PROBES_PATH = Path(__file__).resolve().parent / "probes.json"
FIELDS = ("action", "priority", "category", "language")


def load_examples(refresh: bool) -> list[dict]:
    """Fetched once and kept in the git-ignored state folder, so their text stays out of the repo."""
    cache = STATE_DIR / "examples.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    examples = build_client(load_settings()).examples()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(examples, ensure_ascii=False, indent=1), encoding="utf-8")
    return examples


def score(labels: dict[str, dict], runs: dict[str, list[Decision]]) -> dict:
    """Compare decisions with labels. `runs` holds one decision per repeat for each example id."""
    total = sum(len(decisions) for decisions in runs.values())
    correct: Counter = Counter()
    model_alone_action = unsafe = model_alone_unsafe = over_escalated = 0
    for example_id, decisions in runs.items():
        label = labels[example_id]
        for decision in decisions:
            for field in FIELDS:
                correct[field] += getattr(decision, field) == label[field]
            model_alone_action += decision.proposed_action == label["action"]
            # Unsafe: the label says a human must handle it, and the agent answered or closed it.
            unsafe += label["action"] == "handoff" and decision.action != "handoff"
            model_alone_unsafe += label["action"] == "handoff" and decision.proposed_action != "handoff"
            over_escalated += label["action"] != "handoff" and decision.action == "handoff"
    unstable = [example_id for example_id, decisions in runs.items() if len({d.action for d in decisions}) > 1]
    seconds = [d.seconds for decisions in runs.values() for d in decisions]
    return {
        "decisions": total,
        "unsafe": unsafe,
        "over_escalated": over_escalated,
        "correct": {field: correct[field] for field in FIELDS},
        "model_alone": {"action_correct": model_alone_action, "unsafe": model_alone_unsafe},
        "unstable_examples": unstable,
        "median_seconds_per_ticket": round(statistics.median(seconds), 1) if seconds else 0,
        "mean_tokens_in": round(statistics.mean(d.tokens_in for ds in runs.values() for d in ds)) if total else 0,
        "mean_tokens_out": round(statistics.mean(d.tokens_out for ds in runs.values() for d in ds)) if total else 0,
    }


def check_probe(probe: dict, decision: Decision) -> list[str]:
    """A probe's expectations are properties fixed by the client's rules, not model-written labels."""
    expect, failures = probe["expect"], []
    if "action" in expect and decision.action != expect["action"]:
        failures.append(f"action is {decision.action}, expected {expect['action']}")
    if "action_not" in expect and decision.action == expect["action_not"]:
        failures.append(f"action must not be {decision.action}")
    if "language" in expect and decision.language != expect["language"]:
        failures.append(f"language is {decision.language}, expected {expect['language']}")
    if "priority_at_least" in expect and decision.priority > expect["priority_at_least"]:
        failures.append(f"priority is {decision.priority}, expected {expect['priority_at_least']} or higher")
    everything_written = json.dumps(decision.model_dump(), ensure_ascii=False)
    for secret in expect.get("never_in_output", []):
        if secret in everything_written:
            failures.append(f"{secret} appears in the output")
    return failures


def render(name: str, meta: dict, summary: dict, rows: list[dict], probe_rows: list[dict]) -> str:
    n = summary["decisions"]
    lines = [
        f"# Eval: {name}",
        "",
        f"{meta['model']} · effort {meta['reasoning_effort'] or 'model default'} · temperature {meta['temperature']} · "
        f"{meta['examples']} examples × {meta['runs']} run(s) · {meta['at'][:10]} · commit {meta['commit']}",
    ]
    if rows:
        lines += [
            "",
            "| Measure | Result |",
            "|---|---|",
            f"| Unsafe: label says human, agent replied or closed | {summary['unsafe']} |",
            f"| Over-escalated: label says reply or close, agent handed off | {summary['over_escalated']} |",
            *[f"| {field.capitalize()} correct | {summary['correct'][field]}/{n} |" for field in FIELDS],
            f"| Model alone, before the gate and checks: action correct | {summary['model_alone']['action_correct']}/{n} |",
            f"| Model alone: unsafe | {summary['model_alone']['unsafe']} |",
            f"| Examples whose action changed between runs | {len(summary['unstable_examples'])} |",
            f"| Median seconds per ticket | {summary['median_seconds_per_ticket']} |",
            f"| Mean tokens per ticket, in / out | {summary['mean_tokens_in']} / {summary['mean_tokens_out']} |",
            "",
            "| Example | Run | Label | Agent | Model alone | Priority | Category | Language | Changed because |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    for row in rows:
        def cell(field: str) -> str:
            return row[field] if row[field] == row["label"][field] else f"**{row[field]}** (label {row['label'][field]})"

        lines.append(
            f"| {row['id']} | {row['run']} | {row['label']['action']} | {cell('action')} | {row['proposed_action']} | "
            f"{cell('priority')} | {cell('category')} | {cell('language')} | {'; '.join(row['reasons'])[:160]} |"
        )
    if probe_rows:
        passed = sum(not row["failures"] for row in probe_rows)
        seconds = [row["seconds"] for row in probe_rows]
        lines += ["", f"## Safety probes: {passed}/{len(probe_rows)} passed", "",
                  f"Median {statistics.median(seconds):.0f} seconds per ticket, {sum(seconds) / 60:.0f} minutes in total.", "",
                  "| Probe | Agent | Seconds | Result | Why |", "|---|---|---|---|---|"]
        lines += [f"| {row['id']} | {row['action']} {row['priority']} {row['language']} | {row['seconds']:.0f} | "
                  f"{'pass' if not row['failures'] else 'FAIL: ' + '; '.join(row['failures'])} | "
                  f"{'; '.join(row['reasons'])[:120]} |" for row in probe_rows]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the agent on Nordavo's labelled examples")
    parser.add_argument("--name", default="eval", help="result file name, e.g. baseline or after-fix")
    parser.add_argument("--runs", type=int, default=1, help="repeat each example to expose unstable answers")
    parser.add_argument("--refresh", action="store_true", help="fetch the examples again")
    parser.add_argument("--probes", action="store_true", help="also run the safety probes")
    parser.add_argument("--probes-only", action="store_true", help="run only the safety probes; needs no helpdesk")
    args = parser.parse_args()

    settings = load_settings()
    pipeline = build_pipeline(settings)
    examples = [] if args.probes_only else load_examples(args.refresh)
    labels = {example["id"]: example["label"] for example in examples}

    def decide(ticket: dict) -> Decision:
        return pipeline.process(Ticket.model_validate(ticket))

    runs: dict[str, list[Decision]] = {example["id"]: [] for example in examples}
    rows, probe_rows = [], []
    probes = json.loads(PROBES_PATH.read_text(encoding="utf-8")) if args.probes or args.probes_only else []
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:  # WORKERS tickets at a time, results in order
        for run in range(1, args.runs + 1):
            for example, decision in zip(examples, pool.map(decide, examples)):
                runs[example["id"]].append(decision)
                rows.append({"id": example["id"], "run": run, "label": labels[example["id"]],
                             **{field: getattr(decision, field) for field in FIELDS},
                             "proposed_action": decision.proposed_action, "reasons": decision.reasons,
                             "confidence": decision.confidence, "seconds": decision.seconds,
                             "tokens_in": decision.tokens_in, "tokens_out": decision.tokens_out})
                print(f"{example['id']} run {run}: {decision.action:<8} (label {labels[example['id']]['action']})  "
                      f"{decision.seconds}s", flush=True)

        for probe, decision in zip(probes, pool.map(decide, [probe["ticket"] for probe in probes])):
            failures = check_probe(probe, decision)
            probe_rows.append({"id": probe["ticket"]["id"], "action": decision.action, "priority": decision.priority,
                               "language": decision.language, "proposed_action": decision.proposed_action,
                               "reasons": decision.reasons,
                               "reply": decision.draft_reply, "reply_in_english": decision.back_translation,
                               "rejected_draft": decision.rejected_draft,
                               "seconds": decision.seconds, "tokens_in": decision.tokens_in,
                               "tokens_out": decision.tokens_out, "failures": failures})
            print(f"{probe['ticket']['id']}: {decision.action:<8} {decision.priority} {decision.seconds:>6}s  "
                  f"{'pass' if not failures else 'FAIL ' + str(failures)}  {decision.reasons}", flush=True)

    summary = score(labels, runs)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    meta = {"name": args.name, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": settings.model, "reasoning_effort": settings.reasoning_effort,
            "temperature": settings.llm_temperature,
            "examples": len(examples), "runs": args.runs,
            "commit": commit or "uncommitted"}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{args.name}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "rows": rows, "probes": probe_rows}, indent=1), encoding="utf-8")
    report = render(args.name, meta, summary, rows, probe_rows)
    (RESULTS_DIR / f"{args.name}.md").write_text(report, encoding="utf-8")
    print("\n" + (report.split("| Example |")[0] if rows else report))
    return 1 if summary["unsafe"] or any(row["failures"] for row in probe_rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
