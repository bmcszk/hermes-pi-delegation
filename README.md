# hermes-pi-delegation

A Hermes plugin that lets one agent delegate work to isolated `pi` workers and
enforce the delegation laws mechanically: a worker must cite what it changed, it
must not narrate, and no agent may mutate git history through a tool call.

The plugin is deliberately agnostic. It knows nothing about your project, your
models, or your machine. A *worker type* is a tool policy. A *model* is an
optional alias you may pass on a dispatch; without it the worker runs on pi's
own `defaultModel`. Your Hermes skill supplies the aliases.

## Tools

Both tools return JSON strings. `pi_launch` returns a single-line POSIX shell
command; run it through your terminal tool with `background=true`.

### `pi_launch`

Generates one worker dispatch.

| Parameter | Required | Meaning |
| --- | --- | --- |
| `contract_path` | yes | File holding the worker prompt. |
| `cwd` | yes | Absolute project directory. pi is project scoped. |
| `worker_type` | no | Defaults to `default`, a passthrough with no tool/codemode/thinking policy. `reasoner`, `brainer`, `coder`, `auditor` attach a policy. Unknown non-default names are an error. |
| `skills` | no (recommended) | Skill dirs (`~` or absolute) or bare names. Bare names resolve against `.agents/skills` and `.pi/skills` in the `cwd` ancestry, then `~/.agents/skills`, then `~/.hermes/skills/*`, then `~/.pi/skills`. Discovered paths are emitted absolute. pi does not inherit parent skills, so every skill the worker needs must be listed. Omitted or empty: no `--append-system-prompt` and no `system_prompt` key. |
| `model` | no | Model alias for the worker upstream. Omitted: the command carries no `--model` at all and pi uses its own `defaultModel` from `~/.pi/agent/settings.json`. The plugin never hardcodes one. |
| `epic_id` / `task_slug` | no (recommended) | Session id becomes `<epic_id>-<worker_type>` or `task-<slug>-<worker_type>`. With neither, it becomes `task-<basename of cwd>-<worker_type>`: the same cwd with the same worker_type reuses that session id, and `--session-id` makes pi reuse the session state behind it. |
| `require_edit` | no | Path or list of paths that must change for the run to count as done. Turns on the nudge gate. |
| `min_tool_calls` | no | Tool calls required before the run counts as done. Needs `require_edit`. |
| `max_nudges` | no | Nudges allowed before the gate gives up. Needs `require_edit`. |
| `require_sentinel` | no | String that must appear in the `require_edit` file at exit. The registry row then records `deliverable: met\|unmet\|unchecked`, which `wait` and `list` pass through. Needs `require_edit`. See the sentinel pattern below. |
| `timeout_s` | no | Idle budget per dispatch. Defaults to `PI_WORKER_TIMEOUT_S`, 900 s. |
| `wall_timeout_s` | no | Hard ceiling on the whole dispatch. Defaults to `max(3600, timeout_s + 900)`. |

Preflight validates the dispatch before anything spawns: the `model` alias is
checked against the deployment's own model list (`models.json` plus
`defaultModel`, both read from the worker agent dir — an unknown alias is a
launch error, an unreadable list degrades to a warning, never an assumed
error), omitting `model` draws a warning that the worker runs pi's
`defaultModel`, and `epic_id` + `task_slug` set together are rejected (mutually
exclusive; `epic_id` wins the session id).

---

**Project-agnostic skill**: The delegation laws taught by this plugin are
also shipped as a standalone skill at `skills/pi-delegation/SKILL.md` (and
mirrored in this README). A fresh agent with ONLY the repo can run a gated
dispatch end-to-end following just the skill + README — no external skills
required.

---

Worker types carry tool policy; what each is FOR and what its deliverable is:

| Worker type | Purpose | Deliverable | Tools | codemode | thinking |
| --- | --- | --- | --- | --- | --- |
| `default` | passthrough dispatch | whatever the run's work produces | pi defaults | no | pi default |
| `reasoner` | read-only analysis, opinions in the reply text | reply text | `read,grep,find,ls` | no | high |
| `brainer` | light edits + short docs | edited files + reply text | `read,grep,find,ls,write,edit` | no | high |
| `coder` | implementation work | edited files (nudge-gated) | pi defaults | yes | pi default |
| `auditor` | review/triage jobs under the SAME registry and pi_manage surfaces (list, wait, kill, steer, logs via log_path, salvage_report) — runs as a real dispatch with a row, heartbeats and log_path. Go-to for text-deliverable jobs: the final assistant TEXT is the run's only observable output, so it is written into the first `require_edit` path at completion. The text lands on status done; a failed deliverable write downgrades to `incomplete` (exit 3). Full client-side tracking and management machinery work for it. | text written into the first `require_edit` path | `read,grep,find,ls,write,edit` | no | pi default |

### One-shot dispatch (`auto=true`)

`pi_launch` accepted `auto`: with it, the launcher runs the generated
command itself as a DETACHED background process (its own process group —
the kill path stays exact) and returns
`{dispatched: true, pid, session_id, log}` where `log` is
`<cwd>/.hermes/lane/<session_id>.log`, holding the worker's combined
stdout+stderr (preflight + budget echoes included). The paste path
(`auto=false`, the default) is unchanged: the command comes back as a
string and NOTHING spawns. Preflight still runs FIRST either way — a
preflight failure emits no process in both modes. Truth lives in the
registry: `wait`/`list`/`kill` work on an auto-dispatched worker exactly
like a pasted one, and the log file is the belt-and-suspenders view over
`logs` (log_path).

### `pi_manage`

Registry and process control for running workers.

| Action | Meaning |
| --- | --- |
| `list` | Live and finished dispatches from the registry. Running agents carry `last_activity_s`: seconds since the client's last frame. A large age with a live pid is a stalled or silent worker. |
| `kill` | Kill a dispatch by `session_id`. Own process group, so tool children die too. |
| `doctor` | Report plugin freshness and schema parity between the registered schema and the implementation signature. |
| `logs` | Tail the newest pi session log for a `session_id`. |
| `wait` | Probe the registry until a `session_id` settles or `timeout_s` elapses. `unknown` means no registry entry for the session at all; a registered session always answers `running` (with `pid`, plus `last_activity_s` when a heartbeat age is available) or terminal (with the registry status, `exit_code`, and `deliverable` when the row carries them) — even when a whole-file registry rewrite is mid-flight, so a healthy worker is never mistaken for an unknown one. |
| `steer` | Inject a message into a live worker's current run, or queue it for the next turn with `queue=true`. Landing is confirmed by the worker's next output, not the call. |

#### wait's own ceiling: 1500 s

A host's tool-call layer caps one call (Hermes: 1600 s). A `wait` that rides
that limit does not return a late answer — it loses the turn, and the caller
sees a bare timeout error with no state at all. Six of those cost ~2.6 h in one
live session before this clamp existed.

So `wait` clamps `timeout_s` to `WAIT_CEILING_S` (1500 s), comfortably under
the host's ceiling, and answers at the clamp instead of the requested budget:

```json
{"finished": false, "status": "running", "pid": 4711,
 "requested_timeout_s": 99999, "timeout_s": 1500,
 "clamped": true, "resumed_by": "call-wait-again"}
```

`clamped` says the budget was shortened and `timeout_s` says to what; the
session itself is untouched. `resumed_by: call-wait-again` appears only while
the session is still settling — waiting again is safe, the loop is stateless.
A budget at or under 1500 s comes back unclamped (`clamped: false`, no hint).


### `verify`: one call for the whole exit drill

Exit codes lie in both directions: a run can finish ALL the work and still
exit 3 with `satisfied=False`, while another exits 0 with the sentinel written
but the tree dirty. `pi_manage(action="verify", session_id=...)` answers with
`exit_code`, `uncommitted_files`, `commits_since_base`, `unpushed_commits`, the
re-checked `deliverable`, and a single top-level `ok` verdict that judges the
tree rather than the client's claim — exit 3 over a committed, clean, pushed
tree answers `ok: true` with the contradiction named; exit 0 with residue
answers `ok: false` naming it. Every leg degrades to a named skip (no upstream,
no base, non-git cwd), never an error. It is read-only and distinct from
`salvage_report`: that one is the fast post-exit state dump with per-project
gates and no remote comparison; `verify` adds the upstream comparison and one
verdict.

### `salvage_report` quick gates follow the recorded cwd

`salvage_report` answers with the post-exit state of one dispatch; its `gates`
array is the quick check of the tree the WORKER worked in. Detection reads the
root markers of the row's recorded `cwd`, never the caller's tree — a Go worker
once came back graded by this plugin's own lint, with 23 pre-existing errors
from an unrelated repo leaking into the report.

| Recorded cwd has | `project_type` | Gates (run inside that cwd) |
| --- | --- | --- |
| `go.mod` | `go` | `gofmt -l .` (fails, listing files, when anything is unformatted), `go build ./...`; `go test ./...` only with `go_tests=true` |
| `pyproject.toml` | `python` | `uvx ruff check .`, `python -m compileall -q .` |
| `plugin.yaml` | `plugin` | `uvx ruff check .`, `python -m compileall -q __init__.py scripts tests` (this plugin's own report) |
| no marker | `unknown` | one `project-type` row carrying `skipped: <reason>` — an empty gate list never reads as a pass |

The result also carries `project_type`, so a continuation contract can state
what was checked. `go test` stays behind the flag because it dominates the
report's runtime; the cheap gates above are the ones that catch a broken tree.
A gate that cannot be invoked (command missing, unwritable cwd, 120 s timeout)
degrades to a `skipped` row instead of failing the report.

### The sentinel pattern: a call count is not a deliverable

`min_tool_calls` counts tool calls, and research is made of tool calls. A coder
dispatch with `min_tool_calls=12` exited `0`, `nudges=0`, `satisfied=True` after
35 calls that were ALL upstream `grep`/`read` — the `require_edit` file still
held only its header, and the contract's commit, push and sentinel never
happened. The parent only found out by reading the anchor file by hand.

`require_sentinel` closes that hole. Name a string the contract requires the
worker to write into the deliverable file; at exit the client greps that file
for it and records the verdict:

| Verdict | Meaning |
| --- | --- |
| `met` | the sentinel string is in the `require_edit` file |
| `unmet` | the sentinel is absent, or the file is gone |
| `unchecked` | no `require_sentinel` was requested |

The **exit code is unchanged** — `unmet` is surfaced, not enforced, so a run
that legitimately ends early still reports its real status. The point is that
the parent's FIRST check catches it: `wait` and `list` pass `deliverable`
straight through from the registry row.

Example: a contract whose last step appends `TEXTS1-DONE` to the lane log.

```json
{
  "contract_path": "/abs/contract.md",
  "cwd": "/abs/project",
  "worker_type": "coder",
  "epic_id": "task-textS1",
  "require_edit": ["/abs/project/.hermes/lane/textS1.md"],
  "min_tool_calls": 12,
  "require_sentinel": "TEXTS1-DONE"
}
```

The contract then ends with the sentinel as the worker's last obligation:

> Finish by appending the single line `TEXTS1-DONE` to
> `.hermes/lane/textS1.md`. That line is your claim that the work is done.

A research-only session leaves the file at its header, and `wait` answers:

```json
{"finished": true, "status": "done", "exit_code": 0,
 "deliverable": "unmet"}
```

Launches without `require_sentinel` keep the old shape: no `deliverable` key on
rows that never recorded one. `require_sentinel` without `require_edit` is a
preflight error — the check has no file to grep.

### Notifications and exit codes

Background notifications report the exit code of the **wrapper**, not of the
worker. A trailing `; echo "TAG=$?"` always succeeds and masks the real code;
end the wrapper with the code-preserving form instead:

```sh
rc=$?; echo "TAG=$rc"; exit $rc
```

The client also writes its true terminal status (`done`, `incomplete`,
`timeout`, `killed`) plus `exit_code` into the registry, so the real outcome is
readable regardless of the wrapper: `pi_manage` `list` and `wait` surface the
terminal `exit_code`. `pi_manage` `kill` stamps `exit_code: 143` itself, since
the killed client never gets to write.

| Exit | Meaning |
| --- | --- |
| 0 | done: dispatch satisfied |
| 3 | incomplete: correction budget exhausted (nudged out) |
| 130 | KeyboardInterrupt (Ctrl-C) |
| 143 | SIGTERM (external kill; kill action stamps 143 itself) |

Other non-signal failures exit per Python conventions (traceback to stderr,
non-zero).

### Lane locks

One live dispatch holds one lock per lane: the client writes
`<cwd>/.hermes/lane/LOCK/<session_id>.lock` (JSON: `session_id`, `pid`,
`model`, `started_at`) at dispatch start and removes it on every terminal
exit — done, incomplete, timeout, and the SIGTERM unwind all release it. The
dir sits inside the `.hermes/` ignore region.

A crash that skips removal (SIGKILL, OOM, power loss) leaves a **stale**
lock behind: same file, dead pid. `pi_manage list` surfaces `lane_lock`
(`{present, stale}`) on any registry entry whose lock is on disk, and
`salvage_report` adds the full row — path, payload, staleness, and the exact
`rm` command to remove it (the action itself deletes nothing; removal
deliberately stays a human decision).

While a lane is LIVE, run parent gates only from a DIFFERENT worktree — or
after worker exit. The lock makes that contention visible instead of
silent: a concurrent gate run in the same worktree is exactly the
build-cache collision this lock exists to catch.

### The registry log is line-delimited JSON, end to end

Every dispatch appends its lifecycle rows to `~/.hermes/logs/pi-agents.jsonl`
(`done`/`running`/`killed`/`timeout`), and `list`/`wait`/`salvage_report` read
it back. Concurrent dispatches once tore that file — 4 unparseable lines of 791
— because a second writer could land mid-record and a whole-file rewrite could
swap in a shorter file while an append sat at the old offset.

Now every mutation — append, patch, terminal merge — holds one exclusive lock
(`pi-agents.jsonl.lock`) and replaces the file through a temp file plus an
atomic rename, so a record is whole from any reader's point of view, or it is
not there yet. An append still keeps the single `write()` call for the whole
record plus its newline.

**A torn line never costs you the registry.** Readers skip a line that does not
parse and report it in `_last_registry_warning` (also logged as a warning),
so one truncated record cannot hide every other row from `list`, `wait`, or
`salvage_report`.

**Recovery.** `_registry_repair()` rewrites the registry clean: every
parseable row is kept, every unparseable line is moved verbatim into
`pi-agents.jsonl.corrupt` (append-only — nothing is thrown away) and the file
is replaced atomically. Run it from a python shell in the plugin directory:

```sh
python3 -c 'import __init__ as p; print(p._registry_repair())'
# {'kept': 791, 'quarantined': 4, 'corrupt_path': '...pi-agents.jsonl.corrupt'}
```

A fully parseable registry is left untouched (no rewrite, no mtime change), so
the repair is safe to run as a routine check.

### Worker isolation directory

Every dispatch points pi at `~/.pi/agent-worker` (`WORKER_AGENT_DIR`). The plugin
manages that directory: it holds the same providers and auth as `~/.pi/agent`
but no delegation subagents, plus an `AGENTS.md` that forbids re-delegation.
Change the constant if you want a different isolation dir.

### Parent git-mutation guard

The plugin registers a `pre_tool_call` hook. When a terminal command targets a
git binary and runs a mutating subcommand (`stash`, `reset`, `checkout`,
`switch`, `restore`, `merge`, `rebase`, `revert`, `clean`, `branch -d/-D/-m/-M`,
`push --force`, `push -f`), the call is blocked. This is parent-side only;
worker tool calls never pass through it.

## Install

```sh
hermes plugins install bmcszk/hermes-pi-delegation
```

The plugin has no runtime dependencies. It needs `pi` on `PATH`, or under a
per-user install root (`~/.nvm/versions/node/*/bin`, `~/.npm-global/bin`,
`~/.local/bin`, `~/.bun/bin`).

## First dispatch (copy-paste walkthrough)

A cold-start user with ONLY this repo can run a gated dispatch end-to-end:

**1. Minimal contract** (save as `/abs/project/contract.md`):

```markdown
Task: add a `hello()` function to `lib.py` that returns "world".
Scope: only touch `lib.py` and its tests.
Verification: run `python -c "import lib; assert lib.hello() == 'world'"`.
Deliverable: `lib.py`.

Finish by appending the single line `TASK-DONE` to `.hermes/lane/task.md`.
That line is your claim that the work is done.
```

**2. Paired `pi_launch` JSON**:

```json
{
  "contract_path": "/abs/project/contract.md",
  "cwd": "/abs/project",
  "worker_type": "coder",
  "epic_id": "task-hello",
  "require_edit": ["/abs/project/lib.py", "/abs/project/.hermes/lane/task.md"],
  "require_sentinel": "TASK-DONE"
}
```

**3. Launch → Wait → Read the verdict**:

```sh
# If pi_launch is in your toolset:
pi_launch(...)  # returns {command, session_id, next, ...}
# Run the command through your terminal tool with background=true
pi_manage(action="wait", session_id="task-hello-coder", timeout_s=1500)
# Answers: {finished: true, status, exit_code, deliverable, tree, ...}
# Check: deliverable == "met" AND tree.uncommitted_files == 0 AND tree.unpushed == 0
# If deliverable == "unmet": the sentinel was absent from the deliverable file.
# If exit_code == 0 but tree.uncommitted_files > 0: the exit-0-no-edits trap.
# If exit_code == 3 but tree.uncommitted_files == 0 and tree.unpushed == 0:
#   the work is done despite the client's verdict (verify will say ok: true).
```

**4. If the pi tools are NOT in your toolset** (fallback):

```sh
python3 <plugin>/scripts/pi_rpc_client.py --help
# Flag names drift between versions; --help is the source.
```

**Fields you check in the terminal answer**:

| Field | Meaning |
|-------|---------|
| `status` | `running` / `done` / `incomplete` / `timeout` / `killed` |
| `exit_code` | 0=done, 3=incomplete (nudged out), 130=Ctrl-C, 143=SIGTERM |
| `deliverable` | `met`/`unmet`/`unchecked` (only when `require_sentinel` was set) |
| `tree` | `dirty`, `commits_since_base`, `unpushed` (git facts, never empty) |

Each field's meaning matches the exit-code table above (no contradictions).

## Development

Import constraint under the Hermes loader: modules at the plugin root import
fine, but a module inside `scripts/` must be imported after the guarded
`sys.path` prepend at the top of `__init__.py` — the loader only puts the
plugin root on the module path.

```sh
uv run --with pytest python -m pytest tests -q
uvx ruff check .
```

Layout:

```
__init__.py                  plugin entry: register, pi_launch, pi_manage, hook
plugin.yaml                  Hermes manifest
scripts/pi_rpc_client.py     pi RPC protocol client (newline-delimited JSON)
scripts/sweep_runner.py      batch sweep runner with an enforced canary gate
scripts/verify_run.py        verified dispatch: artifacts plus completion marker
tests/test_plugin.py         plugin handler and command-generation tests
tests/unit/                  client and sweep-runner tests
```

## Model aliases

The plugin contains no model names, no project names, and no machine paths. A
sweep's model aliases are the caller's concern: define them in your Hermes
skill, pass one per dispatch as `model`, and the plugin puts that exact string
on the worker argv. Skip `model` and the worker inherits pi's `defaultModel`.

## Example dispatches

Minimal: every optional parameter takes its default.

```json
{"contract_path": "/abs/contract.md", "cwd": "/abs/project"}
```

That emits `python3 .../pi_rpc_client.py --session-id task-project-default
--cwd /abs/project ...` with no `--model` flag.

Fully specified:

```json
{
  "contract_path": "/abs/contract.md",
  "cwd": "/abs/project",
  "worker_type": "coder",
  "skills": ["coding-rules", "/abs/skills/go-dev"],
  "model": "my-alias",
  "epic_id": "epic-1234",
  "require_edit": ["src/app.go"],
  "timeout_s": 1800
}
```

## Continuous integration

`.github/workflows/pr.yml` runs ruff and pytest on every pull request and on
`push` to `master`. `.github/workflows/security.yml` runs CodeQL for Python.
Both actions are pinned to full commit SHAs, resolved through the GitHub tags
API:

| Action | Version |
| --- | --- |
| `actions/checkout` | v4.4.0 |
| `astral-sh/setup-uv` | v5.4.2 |
| `github/codeql-action` | v4.39.3 |

## License

MIT, copyright 2026 bmcszk. See `LICENSE`.

<!-- ci-trigger -->
