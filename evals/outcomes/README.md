# Outcomes

Each outcome is one experiment: a **baseline**, **one change**, and the **result**. Kept this way, results stop being a table that gets overwritten and become data you can compare across changes and over time.

`evals/results/` is not the record. The runner overwrites `evals/results/<model>.json` on every run, and the folder is gitignored. An outcome freezes copies of its runs, so its baseline is still there after the next run.

## Adding an outcome

1. **Freeze the baseline first.** Copy the current result files somewhere before you change anything, since the next run overwrites them. Or use the result files of an earlier outcome as the baseline.
2. **Make one change.** If other things changed too, list them in `also_changed` (see below). A comparison with three changes in it can't tell you which one mattered.
3. **Run the suite** for each model: `python -m evals.runner --provider ... --model ...`.
4. **Record it:**

   ```bash
   python -m evals.outcomes record 2026-10-01-short-name \
       --question "What are we trying to find out?" \
       --change "The one thing that differs, and where it lives in the code" \
       --baseline path/to/baseline-run.json@<commit> \
       --result evals/results/<model>.json
   ```

   Repeat `--baseline` and `--result` once per model. `@<commit>` says which code produced a run. It defaults to the current `HEAD`, marked `+uncommitted` if tracked files have changes, so pass it explicitly for a run made earlier.

5. **Write the `README.md`** in the new folder: the question, the change, the table (`python -m evals.outcomes table <slug>`), what it shows, and anything that makes the numbers less trustworthy than they look.

`record` also rebuilds `index.csv`. To rebuild it on its own, for example after editing an `outcome.json`, run `python -m evals.outcomes index`.

## What's in an outcome folder

- `README.md` — the write-up, by hand.
- `outcome.json` — the data: `question`, `change`, an optional `also_changed` list, and a `baseline` and a `result` section, each mapping a model to its frozen `file`, the `code` that produced it, and when it ran (`run_on`).
- `runs/baseline/`, `runs/result/` — frozen copies of the per-case result files.

## `index.csv`

One row per run across every outcome, computed from the frozen runs by `metrics()` in `evals/outcomes.py`. Every outcome is measured the same way, so rows from different outcomes can be compared directly.

| Column | Meaning |
|---|---|
| `outcome` | The outcome's folder name: the date it was recorded, then a short name. |
| `change` | The one thing that differs between that outcome's baseline and result. |
| `role` | `baseline` (before the change) or `result` (after it). |
| `provider` | Provider and model, as the runner recorded it, e.g. `ollama:qwen2.5:7b-instruct-q4_K_M`. |
| `code` | The commit that produced the run, `+uncommitted` if the working tree had changes, or a note when no commit applies. |
| `run_on` | When the run started, from its first trace's name. |
| `cases` | How many cases the run contained. |
| `acceptable` | Cases graded *pass* or *correctly declined*. The runner's headline number, and only as strict as each case's checks. |
| `finished` | Cases that ended `completed`: every step of the final plan ran and every wait confirmed its target. |
| `blocked` | Cases that ended `blocked`: a constraint (geofence, altitude cap) refused a command. Correct in the cases that test limits. |
| `finished_plan_1` | Finished cases that finished on the original plan. Empty for runs from before replanning, which recorded no plan count. |
| `finished_plan_2` | Finished cases that needed one replan. Empty before replanning. |
| `finished_plan_3` | Finished cases that needed two replans. Empty before replanning. |
| `passes_without_finishing` | Cases graded *pass* that neither finished nor were blocked. Each one is a case whose checks let a mission that never happened pass. |
| `median_wall_s` | Median wall-clock seconds per case, including the simulator reset. |
| `median_model_s` | Median seconds per case spent waiting on the model: planning, replanning and the closing report. |
| `total_min` | Wall-clock minutes for the whole run. |
| `cost_usd` | API cost of the run in US dollars, from recorded token counts and the price table in `evals/runner.py`. Always 0 for local models. |
| `file` | The frozen run file, relative to the outcome's folder. |

## Outcomes so far

- [`2026-09-28-replanning`](2026-09-28-replanning/README.md) — up to three plans per mission. Sonnet 12/12 on plan 1 both times; qwen2.5:7b 10 → 11 acceptable, 8 → 9 finished or correctly blocked.
