---
name: gatedtrain-operator
description: Drive a gatedtrain project through its MCP tools and stop for the person at every trust decision.
---

# gatedtrain operator

gatedtrain turns a goal, a data file and a pipeline into a trained model with a verdict (PASS, FAIL or INCONCLUSIVE)
that it computes itself from a sealed test, a pre-registered metric and a budget. You drive it; you do not judge it.
The person makes every trust decision.

## Setup (once, by the person)

- Register the server: `claude mcp add gatedtrain -- gatedtrain-agent mcp`
- Install this skill: copy the directory that `gatedtrain-agent skill-path` prints (the folder holding this file) to
  `~/.claude/skills/gatedtrain-operator/`.

## Tools

All paths are absolute. A relative or missing `root` is refused.

- `project_create(root, goal, data, pipeline, keeper?, gpu_hours?)`: make a project at a new root.
- `project_status(root)`: stage states and the gate. Stages come back alphabetically; the real order is the
  `stages` list in `<root>/pipeline.yaml`.
- `stage_plan(root, stage)`: what a stage will do. Read it before every run.
- `stage_run(root, stage, decision?, wait_seconds=60)`: start a stage and wait up to `wait_seconds` (0 to 600).
  `{"running": true}` means it is still going.
- `stage_wait(root, stage, wait_seconds=60)`: keep waiting for a started stage until it returns the finished result.
- `project_report(root)`: the final report.
- `job_list(root)`, `job_logs(root, job_id, tail?)`, `job_cancel(root, job_id)`: inspect or stop a job.

## Order of work

1. `project_status`, then for each stage in the pipeline's order that is not done: `stage_plan`, then `stage_run`
   (and `stage_wait` while it reports running). One stage runs at a time per project.
2. When the plan has `needs_user`, stop and ask the person (see below). Do not run that stage on your own.
3. After the last stage, call `project_report`.

## What the person decides

- `needs_user` is `confirm_metric`: show the person the plan's sentence about the deciding metric first. Only after
  they agree, call `stage_run` with `{"metric_confirmed": true}`; this writes
  `<root>/agent/decisions/<stage>.json` (the stage that needs the confirmation, `preregister` in the shipped packs).
  - If the server refuses because the project has no keeper, tell the person to run
    `gatedtrain-agent do <root>` at a terminal, where they type the confirmation. Never pass it yourself there.
  - If gatedtrain refuses for a missing keeper confirmation (rule AN-18), the person does this step themselves
    (never you): `gatedtrain keeper confirm <root>
    --decision <root>/agent/decisions/<stage>.json`. After they say they did, call `stage_run` again.
- `needs_user` is `data_too_small`: the data is too small for a conclusive test. Ask the person whether to accept an
  inconclusive result. Pass `accept_inconclusive` only after their explicit yes.
- A refusal about the budget (GPU hours) goes to the person. Do not look for a way around it or raise the budget.
- Use only the keeper socket the person gives you. Never start a keeper (`keeper serve`) and never run `setup`.
- GPU jobs, cluster access and credentials are the person's to approve.

## Never

- Never read or write anything under `<root>/trust/`.
- Never run `keeper confirm`, `setup` or anything with sudo.
- Never edit the pipeline, the data or a stage's output to change a result.
- Never quote numbers except from `project_report`. Do not estimate or round them up.

## Results

- A FAIL or INCONCLUSIVE verdict is a valid answer. Report it as it is and explain it from the report.
- Do not rerun, retune or "try again" without the person: every sealed attempt spends alpha, so a retry makes
  the next verdict weaker. Offer options and let the person choose.
- A refused stage (`ok` false with `problems`) is not a crash. Quote the problems and their `fix`, and ask the
  person before changing anything.
- If a tool returns `{"ok": false, "error": ...}`, fix the argument it names or tell the person; do not work around it.
