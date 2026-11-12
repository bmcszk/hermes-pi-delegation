---
description: 'A project-agnostic skill for the hermes-pi-delegation plugin: contract-first delegation, tool-gated workers, sentinel deliverables, wait semantics, salvage doctrine, wall-budget laws, and the raw client fallback. No house vocabulary, no model alias names, no machine paths.'
metadata:
    github-path: skills/pi-delegation
    github-ref: refs/heads/master
    github-repo: https://github.com/bmcszk/hermes-pi-delegation
    github-tree-sha: 0000000000000000000000000000000000000000
name: pi-delegation
---
# pi-delegation skill

This skill teaches the delegation laws enforced by the hermes-pi-delegation
plugin. It is deliberately project-agnostic: no tracker names, no model alias
names, no machine-specific paths. Any agent that loads this skill can run a
gated dispatch end-to-end using only the plugin's tools and this skill.

## The delegation loop

1. **Contract first** - Write a contract file that names:
   - Task and scope guard (what NOT to touch)
   - One verification command the worker must run before finishing
   - Deliverable path (the file that proves the work)
   - Sentinel instruction as the LAST obligation ("append SENTINEL to the file")

2. **Launch** - Call `pi_launch` with:
   - `contract_path` pointing at the contract file
   - `cwd` = absolute project directory (pi is project-scoped)
   - `worker_type` = one of `default|reasoner|brainer|coder|auditor`
   - `require_edit` = path(s) that MUST change for the run to count
   - `require_sentinel` = string the contract requires the worker to write
   - `epic_id` or `task_slug` to name the session (else: `task-<cwd-basename>-<type>`)

3. **Wait with the clamp** - Call `pi_manage(action="wait", session_id=..., timeout_s<=1500)`.
   The host caps tool calls at ~1600 s; wait clamps to 1500 s and answers with
   `clamped: true, resumed_by: "call-wait-again"` when shortened. A clamped
   answer means the worker is still running - call wait again.

4. **Verify the tree yourself** - A completion report is a claim. On exit 0
   with zero edits, the trap is real. On exit 3 with all work committed and
   pushed, the work is done despite the client's verdict. Call
   `pi_manage(action="verify", session_id=...)` for a single verdict judged
   on the tree (uncommitted, commits-since-base, unpushed, deliverable).

5. **Salvage order after ANY exit** - timeout != dead; exit 3 can be a
   completed run; exit 0 with zero edits is the trap. The order:
   - `wait` for the terminal answer
   - `verify` for the tree-judged verdict
   - `salvage_report` for the post-exit state dump with per-project gates
   - Decide: resume (if the worker is alive) or dispatch a mechanical finisher

## Gate mechanics

- `require_edit` + `min_tool_calls` + `max_nudges` arm the correction loop.
- Without `require_edit`, a coder that ends its turn early yields exit 0 with
  zero edits and no gate catches it. The plugin warns when a coder dispatch
  carries no `require_edit` (same warning shape as the no-model warning).
- `min_tool_calls` counts tool calls; research IS tool calls. A session can
  make 35 grep/read calls, change nothing, and still exit satisfied.
- `require_sentinel` closes that hole: name a string the contract requires
  the worker to write into the deliverable file. At exit the client greps
  that file and records `deliverable: met|unmet|unchecked`. The exit code is
  unchanged - `unmet` is surfaced, not enforced.

## Sentinel pattern

The contract MUST end with the sentinel as the worker's last obligation:

> Finish by appending the single line `TASK-DONE` to
> `.hermes/lane/task.md`. That line is your claim that the work is done.

If the contract never mentions the sentinel, the worker has no instruction to
write it, and `deliverable: unmet` is guaranteed on an otherwise-successful
run. The plugin cross-checks at launch: if `require_sentinel` is set and the
string does not appear in the contract file, a warning is emitted.

The sentinel's blind spot: it proves LOG work (the worker wrote the line), not
TREE work (the file was committed and pushed). The tree facts on exit 0
overrule the sentinel: call `verify` and check `uncommitted_files`.

## Wait semantics

- `timeout_s` is the per-frame IDLE budget (worker silent too long).
- `WALL_TIMEOUT_S` (default 3600 s) is the hard ceiling on the WHOLE dispatch
  even when a hung tool keeps trickling progress frames.
- `wait` clamps `timeout_s` to 1500 s (WAIT_CEILING_S), safely under the
  host's 1600 s tool-call ceiling.
- `clamped: true` means the budget was shortened; `timeout_s` says to what.
- `resumed_by: call-wait-again` appears only while the session is still
  settling - waiting again is safe, the loop is stateless.

## Salvage doctrine

- **Timeout != dead**. A wall-kill leaves the worktree exactly as it was.
  The client writes a size-capped `git diff` snapshot to
  `<cwd>/.hermes/lane/<session>.salvage.patch` before teardown on
  timeout/killed/incomplete exits. The patch path and a short status head are
  recorded on the registry row (`salvage_patch`, `salvage_status`).
- **Exit 3 can be a completed run**. The client's `satisfied=False` only
  means the nudge gate exhausted its budget. The tree may be fully committed
  and pushed. `verify` answers `ok: true` with the contradiction named.
- **Exit 0 with zero edits is the trap**. The client's `satisfied=True` and
  exit 0 only mean the worker ended its turn. `verify` answers `ok: false`
  naming the uncommitted residue.
- **Salvage order after any exit**: `wait` → `verify` → `salvage_report`.
  The `salvage_report` answer carries the post-exit state (git status, branch
  head, commits since base, diff stat, frames, lane lock, project type, quick
  gates). Gates run inside the WORKER'S recorded cwd, never the caller's.

## Wall-budget laws

- Idle cap (`timeout_s`) must be below wall cap (`wall_timeout_s`).
- Default wall = max(3600, timeout_s + 900) so long sweeps don't die mid-contract.
- Checkpoint commits so a wall kill costs one phase, not the whole sweep.
- Contract state line for resumability: "State at checkpoint: X done, Y
  remaining" so a mechanical finisher can resume without re-reading context.

## Worker-type selection (tool policies)

| Type      | Purpose                    | Deliverable        | Tools                          | codemode | thinking |
|-----------|----------------------------|--------------------|--------------------------------|----------|----------|
| default   | passthrough                | whatever the run produces | pi defaults              | no       | pi default |
| reasoner  | read-only analysis         | reply text         | read,grep,find,ls              | no       | high     |
| brainer   | light edits + short docs   | edited files + reply text | read,grep,find,ls,write,edit | no       | high     |
| coder     | implementation work        | edited files (nudge-gated) | pi defaults              | yes      | pi default |
| auditor   | review/triage (text output) | text written into first `require_edit` path | read,grep,find,ls,write,edit | no       | pi default |

The `auditor` type runs as a real dispatch with a registry row, heartbeats,
and log_path. The final assistant TEXT is the run's only observable output,
so it is written into the first `require_edit` path at completion.

## Session naming and one worker per project

- `epic_id` → session id `<epic_id>-<worker_type>`
- `task_slug` → session id `task-<slug>-<worker_type>`
- Neither → session id `task-<basename of cwd>-<worker_type>` (reuses state)
- Launch blocks a same-id LIVE session, but warns on:
  - Session id reuse with prior finished runs (pi resumes that context)
  - Another live worker on the same cwd (shared registry/transcript state)
  - Another live worker on the same agent dir (shared registry rewrite,
    heartbeat/steer namespace, transcript discovery)
- Both are warnings; neither blocks a legitimate dispatch.

## Liveness via transcript growth + last_activity

- A live worker stamps a heartbeat file every ~5 s. `pi_manage list` surfaces
  `last_activity_s` = seconds since the client's last frame.
- A large age with a live pid is a stalled or silent worker.
- Transcript discovery prefers: filename contains session id → leading bytes
  contain session id → mtime newest. Two concurrent id-less candidates remain
  ambiguous.

## Kill semantics (process group)

- `pi_manage(action="kill", session_id=...)` sends SIGTERM to the client's
  process group (client + pi + tool children).
- The killed client never writes its terminal line; the plugin stamps
  `exit_code: 143` itself.
- `close()` does SIGTERM, wait 5 s, then SIGKILL if still alive.

## Loader-shaped import testing for plugin authors

The Hermes loader imports the plugin root with `module.__path__=[<plugin_dir>]`
only; `scripts/` is NOT on sys.path. The top of `__init__.py` prepends the
scripts dir before importing `registry_log`. Tests must do the same:

```python
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/scripts")
import registry_log
```

## Raw-client fallback when plugin tools are down

If the pi tools are not in your toolset, run the client script directly:

```sh
python3 <plugin>/scripts/pi_rpc_client.py --help
```

Flag names drift between versions; `--help` is the source. The client accepts
`--session-id`, `--model`, `--cwd`, `--skill`, `--append-system-prompt`,
`--codemode`, `--thinking`, `--tools`, `--no-session`, `--agent-dir`,
`--timeout`, `--wall-timeout`, `--require-edit`, `--min-tool-calls`,
`--require-sentinel`, `--auditor-deliverable`, `--max-nudges`, `--prompt`.