#!/usr/bin/env python3
"""Outcomes: a baseline, one change, and the result, kept as data.

    python -m evals.outcomes record 2026-09-28-replanning \\
        --question "Does replanning change the results?" \\
        --change "Up to three plans per mission (harness/replan.py)" \\
        --baseline evals/results/pre_replan/anthropic-claude-sonnet-5.json@pre-git \\
        --result evals/results/anthropic-claude-sonnet-5.json
    python -m evals.outcomes table 2026-09-28-replanning     # markdown table
    python -m evals.outcomes index                            # rebuild index.csv

A result file in evals/results/ is overwritten by the next run of the same model,
so it cannot be a baseline. `record` freezes copies of the runs inside the
outcome's folder and writes outcome.json saying what changed and which code
produced each side. `index` recomputes one row per run, for every outcome, into
evals/outcomes/index.csv: the dataset that grows as outcomes are added. Every
number in it comes from metrics() below, so a table in one outcome and a row
from another are always measured the same way.
"""
import argparse
import csv
import json
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTCOMES = REPO / "evals" / "outcomes"
INDEX = OUTCOMES / "index.csv"
ROLES = ("baseline", "result")

INDEX_COLUMNS = [
    "outcome", "change", "role", "provider", "code", "run_on", "cases",
    "acceptable", "finished", "blocked", "finished_plan_1", "finished_plan_2",
    "finished_plan_3", "passes_without_finishing", "median_wall_s",
    "median_model_s", "total_min", "cost_usd", "file",
]


def metrics(results):
    """The numbers every outcome reports, from one run's per-case results.

    `finished` and `acceptable` are kept apart on purpose: a case can be graded
    acceptable without the mission happening, and the gap between the two is
    one of the things worth watching.
    """
    has_plans = any("succeeded_on_plan" in case for case in results)
    finished_on = [case.get("succeeded_on_plan") for case in results
                   if case.get("succeeded_on_plan")]
    passes_without_finishing = [
        case["case"] for case in results
        if case["verdict"] == "pass"
        and case["stop_reason"] not in ("completed", "blocked")]
    return {
        "cases": len(results),
        "acceptable": sum(case["verdict"] in ("pass", "declined")
                          for case in results),
        "finished": sum(case["stop_reason"] == "completed" for case in results),
        "blocked": sum(case["stop_reason"] == "blocked" for case in results),
        # None, not 0, for runs from before replanning: they recorded no plan
        # count, and a zero would claim something they never measured.
        **{f"finished_plan_{n}": (finished_on.count(n) if has_plans else None)
           for n in (1, 2, 3)},
        "passes_without_finishing": passes_without_finishing,
        "median_wall_s": round(statistics.median(
            case.get("duration_s", 0) for case in results)),
        "median_model_s": round(statistics.median(
            case.get("model_seconds", 0) for case in results)),
        "total_min": round(sum(case.get("duration_s", 0)
                               for case in results) / 60),
        "cost_usd": round(sum(case.get("cost_usd", 0) for case in results), 2),
    }


def run_on(results):
    """When the run happened, from its first trace's name (run-YYYYMMDD-HHMMSS).

    Result files carry no timestamp of their own, and a file's modification
    time changes the moment it is copied.
    """
    for case in results:
        found = re.search(r"run-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})",
                          case.get("trace", ""))
        if found:
            y, mo, d, h, mi = found.groups()
            return f"{y}-{mo}-{d} {h}:{mi}"
    return ""


def current_code():
    """HEAD, marked dirty if tracked files have uncommitted changes."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain",
                                "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head.stdout.strip() + ("+uncommitted" if dirty.stdout.strip() else "")


def load(path):
    return json.loads(Path(path).read_text())


# --- record -----------------------------------------------------------------

def parse_run(spec):
    """PATH or PATH@CODE. CODE says which code produced a run recorded later."""
    path, _, code = spec.partition("@")
    return Path(path), code or None


def record(slug, question, change, baselines, results):
    folder = OUTCOMES / slug
    outcome = {"slug": slug, "question": question, "change": change}
    for role, specs in (("baseline", baselines), ("result", results)):
        runs = {}
        for spec in specs:
            source, code = parse_run(spec)
            data = load(source)
            provider = data[0]["provider"]
            frozen = folder / "runs" / role / source.name
            frozen.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, frozen)
            runs[provider] = {"file": str(frozen.relative_to(folder)),
                              "code": code or current_code(),
                              "run_on": run_on(data)}
        outcome[role] = runs
    (folder / "outcome.json").write_text(json.dumps(outcome, indent=2) + "\n")
    rebuild_index()
    return outcome


# --- read back --------------------------------------------------------------

def outcomes():
    for path in sorted(OUTCOMES.glob("*/outcome.json")):
        yield path.parent, json.loads(path.read_text())


def rows(folder, outcome):
    for role in ROLES:
        for provider, run in outcome.get(role, {}).items():
            numbers = metrics(load(folder / run["file"]))
            yield {"outcome": outcome["slug"], "change": outcome["change"],
                   "role": role, "provider": provider, "code": run["code"],
                   "run_on": run["run_on"], "file": run["file"], **numbers}


def rebuild_index():
    with INDEX.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        for folder, outcome in outcomes():
            for row in rows(folder, outcome):
                row = dict(row)
                row["passes_without_finishing"] = len(
                    row["passes_without_finishing"])
                writer.writerow({k: ("" if row[k] is None else row[k])
                                 for k in INDEX_COLUMNS})


def table(slug):
    folder = OUTCOMES / slug
    outcome = json.loads((folder / "outcome.json").read_text())
    lines = ["| Model | Acceptable | Really finished | Blocked by a limit "
             "(correct) | Finished on plan 1 / 2 / 3 | Median time per case "
             "| Total | Cost |",
             "|---|---|---|---|---|---|---|---|"]
    by_model = {}
    for row in rows(folder, outcome):
        by_model.setdefault(row["provider"], []).append(row)
    for provider, model_rows in by_model.items():
        for row in model_rows:
            plans = (" / ".join(str(row[f"finished_plan_{n}"]) for n in (1, 2, 3))
                     if row["finished_plan_1"] is not None else "—")
            cells = [f"{provider.split(':', 1)[-1]}, {row['role']}",
                     f"{row['acceptable']}/{row['cases']}", str(row["finished"]),
                     str(row["blocked"]), plans, f"{row['median_wall_s']} s",
                     f"{row['total_min']} min", f"${row['cost_usd']:.2f}"]
            if row["role"] == "result":
                cells = [f"**{cell}**" for cell in cells]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    make = commands.add_parser("record", help="freeze runs and write outcome.json")
    make.add_argument("slug", help="folder name, e.g. 2026-09-28-replanning")
    make.add_argument("--question", required=True)
    make.add_argument("--change", required=True,
                      help="the one thing that differs between baseline and result")
    make.add_argument("--baseline", action="append", required=True,
                      metavar="PATH[@CODE]")
    make.add_argument("--result", action="append", required=True,
                      metavar="PATH[@CODE]")

    show = commands.add_parser("table", help="print an outcome's markdown table")
    show.add_argument("slug")

    commands.add_parser("index", help="rebuild index.csv from every outcome")

    args = parser.parse_args()
    if args.command == "record":
        record(args.slug, args.question, args.change, args.baseline, args.result)
        print(table(args.slug))
    elif args.command == "table":
        print(table(args.slug))
    else:
        rebuild_index()
        print(f"wrote {INDEX.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
