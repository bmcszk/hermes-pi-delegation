---
name: pi-delegation
description: "Use when dispatching work to a pi worker or controlling a running one with the pi-delegation plugin: launch a dispatch, wait for completion, detect a stalled worker, steer a live run, or salvage a dead one. Not for plugin installation or development; see the repo README."
---

# Pi delegation

One agent hires a pi worker. `pi_launch` builds the dispatch command,
`pi_manage` controls the running worker. Installation and setup live in the
repo README; this skill is only the control loop.

## The loop

1. `pi_launch(contract_path, cwd, skills, require_edit=<exact file>)` returns
   a one-line command. Run it verbatim in the background; never hand-write it.
2. `pi_manage(action="wait", session_id=<id>)` blocks until the run settles.
   Never sleep and poll.
3. Verify the work on disk yourself: diff, gates, test counts. A completion
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
- Commit one unit at a time so partial progress survives a timeout.
- Keep each write payload small. One giant write dies mid-JSON and repeats
  until the correction budget is gone.

## State files

| Path | Written by | Read by |
| --- | --- | --- |
| `~/.hermes/logs/pi-agents.jsonl` | client | `list`, `kill`, `wait` |
| `~/.hermes/logs/pi-heartbeat-<session>.json` | client | `list` |
| `~/.hermes/logs/pi-steer/<session>.json` | `steer` | client |
