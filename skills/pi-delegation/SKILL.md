---
name: pi-delegation
description: Delegate work to isolated pi workers with the pi-delegation plugin. Covers launch, wait, steer, kill, and salvage for any project and machine.
---

# Pi delegation

One agent hires a pi worker. The worker edits code, runs tools, and reports.
The plugin owns the handoff: `pi_launch` builds the dispatch command,
`pi_manage` controls the running worker. This skill is the control loop.

## Install

1. Install the plugin into your agent runtime (Hermes, or any runtime that
   loads it): the plugin lives in this repo at the repo root
   (`__init__.py`, `plugin.yaml`, `scripts/`).
2. Give workers an isolated agent dir. Every launch points pi at
   `~/.pi/agent-worker`. It needs `settings.json`, and `auth.json` and
   `models.json` symlinked from your main pi dir. See the plugin README.
3. Install this skill for any agent that will dispatch workers:

   ```
   skills add bmcszk/hermes-pi-delegation
   ```

## The loop

```
pi_launch -> run the echoed command verbatim (background) -> pi_manage wait
```

1. Call `pi_launch` with a contract file (the worker prompt), a `cwd`, and
   `skills`. `require_edit` plus `min_tool_calls` arms the nudge gate.
2. Run the echoed command through your terminal tool with `background=true`.
   Never hand-write the command. Never re-type it from memory.
3. Block on the run with `pi_manage(action="wait", session_id=<id>)`. Do not
   sleep and poll. `wait` settles when the run ends.
4. Verify the work on disk yourself: `git status`, gates, test counts. A
   worker report is a claim, not a result.

## Watch liveness

A pi worker can hang forever while looking busy: a hung tool call still
trickles progress frames, so the per-frame idle timeout never fires. The
client stamps a heartbeat file on every frame (one write per 5 s).

`pi_manage(action="list", only_running=true)` reports `last_activity_s` for
each running worker. A large age with a live pid means the worker is stalled.
When you find one: kill it, check the tree on disk, then relaunch.

## Steer a live worker

```
pi_manage(action="steer", session_id=<id>, message="commit what you have")
````

The message lands in the run in progress on the worker's next output frame.
Add `queue=true` to hold it for the worker's next turn instead.
Steering is fire-and-forget: verify by reading the worker's next output, not
by the steer call itself.

## Kill

```
pi_manage(action="kill", session_id=<id>)
````

Kill takes down the client, the pi process, and every tool child in the
process group.光辉 Kill beats waiting: a same-session prompt into a stalled
worker fails with "already processing". After a kill, relaunch on a FRESH
session id whose contract opens with a state-on-disk section.

## Salvage order

After any exit (done, timeout, kill, exit 3), read the disk before the exit
code:

1. `git status --porcelain` and `git log` since the base.
2. Green work-in-progress: gate it, commit it with the contract trailer.
3. Red or half-done work: leave it in place and name the exact state in the
   continuation contract. Killing without stashing destroys paid work.
4. Exit 3 (nudge-capped) is salvage, not failure. Zero edits plus exit 0 is
   the opposite trap: believe the tree, not the words.

## Contract minimums

- One deliverable per dispatch. Long features get chained slices.
- `require_edit` takes one exact file path, never a directory.
- Put the contract's fingerprint line in the final commit body, so proof of
  edit survives on the pushed branch.
- Commit one unit at a time; partial progress must survive a timeout.
- Keep every write call's payload small. One giant write dies mid-JSON and
  repeats until the nudge budget is gone.

## Files the loop writes

| Path | Written by | Read by |
| --- | --- | --- |
| `~/.hermes/logs/pi-agents.jsonl` | client | `pi_manage` list/kill/wait |
| `~/.hermes/logs/pi-heartbeat-<session>.json` | client | `pi_manage` list |
| `~/.hermes/logs/pi-steer/<session>.json` | `pi_manage` steer | client |

## Notes

- Schema changes (new actions or parameters) need a runtime restart after
  install; code changes hot-reload where the runtime supports it.
- `pi_launch` accepts model aliases per dispatch and hardcodes none. Your
  deployment owns the alias catalog.
