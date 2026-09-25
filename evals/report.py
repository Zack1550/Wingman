#!/usr/bin/env python3
"""Turn eval results into the table that goes in the README.

    python -m evals.report                      # every result file found
    python -m evals.report --markdown           # paste-ready

A pass rate with no denominator and no date is a boast. Every table this prints
carries the model, the case count, and which cases were held out, because the
number is only meaningful next to what produced it.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from evals.grading import DECLINED, FAIL, PASS

RESULTS = Path(__file__).resolve().parent / "results"


def load(directory=RESULTS):
    runs = {}
    for path in sorted(Path(directory).glob("*.json")):
        runs[path.stem] = json.loads(path.read_text())
    return runs


def tally(results):
    counts = {PASS: 0, DECLINED: 0, FAIL: 0}
    for result in results:
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    return counts


def by_family(results):
    grouped = defaultdict(lambda: {PASS: 0, DECLINED: 0, FAIL: 0})
    for result in results:
        grouped[result["family"]][result["verdict"]] += 1
    return dict(grouped)


def rate(counts):
    total = sum(counts.values())
    if not total:
        return "-"
    acceptable = counts[PASS] + counts[DECLINED]
    return f"{acceptable}/{total} ({100 * acceptable / total:.0f}%)"


def spend(results):
    """Totals, and the median rather than the mean.

    One mission that retried an arm for forty seconds should not make the
    typical case look slow.
    """
    costs = [r.get("cost_usd", 0) or 0 for r in results]
    model_times = sorted(r.get("model_seconds", 0) or 0 for r in results)
    wall = sorted(r.get("duration_s", 0) or 0 for r in results)
    median = (model_times[len(model_times) // 2] if model_times else 0)
    median_wall = (wall[len(wall) // 2] if wall else 0)
    return {
        "cost": sum(costs),
        "per_case": sum(costs) / len(costs) if costs else 0,
        "median_model_s": median,
        "median_wall_s": median_wall,
        "input": sum(r.get("input_tokens", 0) or 0 for r in results),
        "output": sum(r.get("output_tokens", 0) or 0 for r in results),
    }


def markdown(runs):
    lines = ["| Model | Cases | Acceptable | Fail | Median model time | "
             "Median wall time | Cost | Cost/case |",
             "|---|---|---|---|---|---|---|---|"]
    for label, results in sorted(runs.items()):
        counts = tally(results)
        money = spend(results)
        model = results[0]["provider"] if results else label
        held_out = " *(reserved)*" if label.endswith("reserved") else ""
        lines.append(
            f"| `{model}`{held_out} | {len(results)} | {rate(counts)} | "
            f"{counts[FAIL]} | {money['median_model_s']:.0f} s | "
            f"{money['median_wall_s']:.0f} s | ${money['cost']:.2f} | "
            f"${money['per_case']:.3f} |")
    lines.append("")
    lines.append("Median model time is the model thinking and answering. "
                 "Median wall time includes the ~90 s simulator reset every "
                 "trial needs, which is a property of this laptop rather than "
                 "of the model.")

    lines.append("")
    lines.append("Per family:")
    lines.append("")
    families = sorted({r["family"] for results in runs.values() for r in results})
    header = "| Family | " + " | ".join(sorted(runs)) + " |"
    lines.append(header)
    lines.append("|---" * (len(runs) + 1) + "|")
    for family in families:
        row = [family]
        for label in sorted(runs):
            grouped = by_family(runs[label])
            row.append(rate(grouped[family]) if family in grouped else "-")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def plain(runs):
    lines = []
    for label, results in sorted(runs.items()):
        counts = tally(results)
        money = spend(results)
        lines.append(f"{label}: {rate(counts)} acceptable "
                     f"({counts[PASS]} pass, {counts[DECLINED]} declined, "
                     f"{counts[FAIL]} fail)")
        lines.append(f"  ${money['cost']:.2f} total, "
                     f"${money['per_case']:.3f}/case, "
                     f"{money['input']:,} in + {money['output']:,} out, "
                     f"median {money['median_model_s']:.0f}s model / "
                     f"{money['median_wall_s']:.0f}s wall")
        for result in results:
            mark = {PASS: "  pass    ", DECLINED: "  declined",
                    FAIL: "  FAIL    "}[result["verdict"]]
            lines.append(
                f"{mark} {result['case']:30s} "
                f"{result.get('model_seconds', 0):5.1f}s model  "
                f"${result.get('cost_usd', 0):.3f}  {result['stop_reason']}")
            if result["verdict"] == FAIL:
                for check in result["checks"]:
                    if not check["ok"]:
                        lines.append(f"             x {check['name']}: "
                                     f"{check['detail'][:90]}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--dir", default=str(RESULTS))
    args = parser.parse_args()

    runs = load(args.dir)
    if not runs:
        print("no results yet")
        return 1
    print(markdown(runs) if args.markdown else plain(runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
