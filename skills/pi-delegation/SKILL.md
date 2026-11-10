---
name: pi-delegation
description: "Use when dispatching work to a pi worker or controlling a running one with the pi-delegation plugin: launch a dispatch, wait for completion, detect a stalled worker, steer a live run, or salvage a dead one. Not for plugin installation or development; see the repo README."
---

# Pi delegation

One agent hires a pi worker. `pi_launch` builds the dispatch command,
`pi_manage` controls the running worker. Installation and setup live in the
repo README; this skill is only the control loop.

## The loop

1. Write the contract file first: a markdown worker prompt with the task, a
   scope guard (what NOT to touch), the gates to run, and the definition of
   done. The worker sees nothing else, so state that must survive belongs in
   the contract, not in chat.
2. `pi_launch(contract_path, cwd, require_edit=<exact file>)` returns a
   one-line command. Run it verbatim in the background; never hand-write it.
   Pick the worker type by job: `coder` edits code, `brainer` writes
   verdicts and docs, `reasoner` is read-only. Set `wall_timeout_s` as the
   hard ceiling per dispatch; `min_tool_calls` and `max_nudges` arm the
   correction loop (a worker that edits nothing is nudged up to the budget,
   then exits 3). `epic_id` and `task_slug` name the session; run one
   worker per project at a time.
3. `pi_manage(action="wait", session_id=<id>)` blocks until the registry
   line is terminal and the pid is gone. Never sleep and poll. `wait` can
   answer `finished: false` with `status: unknown` while the worker is
   alive; treat that as no signal and re-check via `list` `last_activity_s`
   once the registry line lands, or wait on the client process itself.
4. Verify the work on disk yourself: diff, gates, test counts. A completion
   report is a claim, not a result.

## Liveness

A hung tool call still trickles frames, so a worker can burn wall time while
looking busy. `pi_manage(action="list", only_running=true)` reports
`last_activity_s` per running worker: seconds since the client last framed.
A large age with a live pid means stalled or silent. Kill it, read the tree
on disk, relaunch fresh.

## Steer

`pi_manage(action="steer", session_id=<id>, message="...")` injects guidance
into the run in progress; `queue=true` holds it for the next turn. Verify
the message landed by the worker's next output, never by the call itself.

## Reading logs

`pi_manage(action="logs", session_id=<id>, lines=<n>)` tails the newest pi
session log for a dispatch. Read it when `wait` returns a weird status or
the tree looks wrong: the log holds the worker's last frames, tool calls,
and its own exit summary.

## Kill and salvage

`pi_manage(action="kill", session_id=<id>)` stops the client, the pi process,
and all tool children in the process group. Prompting a stalled session
again fails with "already processing": kill first, then relaunch on a fresh
session id whose contract opens with the on-disk state.

Salvage order after any exit: `git status` and `git log` since the base
first, then gates. Green work in progress gets committed; red work stays in
place with its exact state named in the continuation contract. Exit 3 is
salvage, not failure. Exit 0 with zero edits is the opposite trap: believe
the tree, not the words.

## Contract minimums

- One deliverable per dispatch; long work gets chained slices.
- `require_edit` takes one exact file path, never a directory.
- Aim `require_edit` at a file the contract itself orders the worker to
  create or modify; aiming it at files whose edits predate the dispatch
  burns the whole nudge budget on a satisfied tree and exits 3.
- Commit one unit at a time so partial progress survives a timeout.
- Keep each write payload small. One giant write dies mid-JSON and repeats
  until the correction budget is gone.
- Stage via explicitly enumerated `git add <paths>`: a bare `add`/`-A` sweep
  ships unrelated churn (state-file attr changes, lane artifacts) into the
  commit and onto a pushed remote that no rewrite can clean without a force.
- A prohibition the worker must not violate gets a mechanical gate (an
  ignored path, a deny hook, a pre-commit check), never contract text alone.
- A worker that stops with a blocked/spec-defect verdict plus partial commits
  is acting correctly: believe the diagnosis, fix the contract and relaunch.
- Embed a script in a contract only after mentally running each parse branch
  against the manifest's shortest and longest lines; positional field-binding
  bugs (a two-field line against a three-field `read`) hide there and
  half-land before any worker catch.

## State files

| Path | Written by | Read by |
| --- | --- | --- |
| `~/.hermes/logs/pi-agents.jsonl` | client | `list`, `kill`, `wait` |
| `~/.hermes/logs/pi-heartbeat-<session>.json` | client | `list` |
| `~/.hermes/logs/pi-steer/<session>.json` | `steer` | client |
