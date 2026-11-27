---
name: gatedtrain-operator
description: Drive a gatedtrain project through its MCP tools and stop for the person at every trust decision.
---

# gatedtrain operator

gatedtrain turns a goal, a data file and a pipeline into a trained model with a verdict (PASS, FAIL or INCONCLUSIVE)
that it computes itself from a sealed test, a pre-registered metric and a budget. You drive it; you do not judge it.
The person makes every trust decision. This text matches gatedtrain 0.7.1 and gatedtrain-pack-vlm-clevr 0.3.1; read
what a newer release prints before relying on a detail here.

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

## Keeper mode: say which one produced the result

The keeper's mode is observed by gatedtrain (never set by an option) and is printed as a `keeper mode:` line.
- **same-user** (default of `gatedtrain setup keeper`, no sudo): the keeper runs as the person's own OS user and follows
  the procedure, but the sealed data is NOT sealed from that account; anything running as the person, including you
  with a shell, could read it. A PASS carries the qualifier "procedure followed; sealed data not protected from the
  user's account".
- **separate-user** (`gatedtrain setup keeper --mode separate-user`, one sudo for the person): the keeper has its own
  OS user; this is the only mode whose result `gatedtrain verify` accepts with exit 0.
- `gatedtrain verify` on a same-user result prints a warning naming the mode and exits 1. Only the person may rerun it
  with `--accept-mode same-user`: that exits 0 for exactly that result and ends with `ACCEPTED WEAKER MODE: same-user
  (...)`. Never pass `--accept-mode` yourself. A project created with `--require-keeper-mode separate-user` exits 3
  on same-user evidence, option or not.
- Quote the mode line in every summary of a result. Never call a same-user result sealed or hardened.

## Greedy decoding (pack 0.3.1 default)

- The VLM pack registers greedy decoding explicitly: `decoding: greedy`, temperature 0, `top_p` 1.0 and
  one evaluation seed (so K=1, one sample per item). temperature 0 alone does not register it, and several seeds are refused.
- The keeper checks it (AN-26): it generates every sealed item once more under an unregistered control seed and
  compares. Any difference refuses the attempt, settles it as not passed and costs its alpha.
- The pack's generator therefore runs one request at a time (`max_num_seqs=1`, prefix caching off). Do not change
  it, the vLLM version or the GPU without the person: the determinism probe has to be rerun first. A greedy score is not
  comparable with one at temperature 0.7.

## Images and owner-side settings (the person's, never yours)

- Trainer images come from gatedtrain's image lock: `data/image-lock.json` shipped with the release, plus the person's
  own `images.json` (under `$XDG_CONFIG_HOME/gatedtrain/`) added with `gatedtrain image add`. `gatedtrain --json image list`
  shows what is accepted. Never add, widen or edit an image or the lock to get a stage through; no variable or flag
  supplies an accepted image, so a refusal goes to the person.
- Owner settings live in the person's host file (`host.toml` under `$XDG_CONFIG_HOME/gatedtrain/`): the bounds
  `gpu_cap_h` and `gpu_mem_limit_gb`, `input_roots`, `interpreter_trees`, `interpreter`, `mirror_root` and
  `[packs.<name>]` tables (for example `runtime_dir`); the keeper's `--eval-python` and `--gpu-wait-s` are set when the
  person starts it. Never write these files or raise a bound.
- The `GATEDTRAIN_EXT_*` variables and a pack's runtime variables are deprecated and removed in 0.9.0; `ext-*`
  commands print a notice naming the host file key that replaces each. If you see one, tell the person.

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
