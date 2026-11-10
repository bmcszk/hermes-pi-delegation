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
| `timeout_s` | no | Idle budget per dispatch. Defaults to `PI_WORKER_TIMEOUT_S`, 900 s. |
| `wall_timeout_s` | no | Hard ceiling on the whole dispatch. Defaults to `max(3600, timeout_s + 900)`. |

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
| `wait` | Probe the registry until a `session_id` settles or `timeout_s` elapses. `unknown` means no registry entry for the session at all; a registered session always answers `running` (with `pid`, plus `last_activity_s` when a heartbeat age is available) or terminal (with the registry status and `exit_code` when the row carries one) — even when a whole-file registry rewrite is mid-flight, so a healthy worker is never mistaken for an unknown one. |
| `steer` | Inject a message into a live worker's current run, or queue it for the next turn with `queue=true`. Landing is confirmed by the worker's next output, not the call. |

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

## Companion skill for Hermes

The repo ships a skill that covers the delegation loop for Hermes agents:
launch, wait, steer, kill, salvage. Install it with the skills CLI,
targeting Hermes and installing globally:

```sh
npx skills add bmcszk/hermes-pi-delegation --skill pi-delegation -a hermes-agent -g
```

`-a hermes-agent` installs into `~/.hermes/skills/`. The command works once
this skill lands in the default branch (the CLI clones `master`).

## Development

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
