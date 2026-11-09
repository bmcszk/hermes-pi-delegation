# Security Policy

## Supported versions

The `master` branch and the newest released tag receive fixes. Older tags do not.

## Reporting a vulnerability

Open a private security advisory at
https://github.com/bmcszk/hermes-pi-delegation/security/advisories/new

Do not open a public issue for a vulnerability. Expect an initial reply within
seven days. Include the plugin version, the pi version, and a reproduction: the
`pi_launch` arguments and the generated command are usually enough.

## Scope

This plugin generates shell commands and spawns `pi` worker processes on the
local machine. Reports that matter most:

- Command injection through `skills`, `contract_path`, `cwd`, `model`, or `epic_id`.
- A path that escapes the parent git-mutation guard.
- Worker processes that survive `pi_manage(action="kill")`.

## Out of scope

- Behaviour of the `pi` binary itself. Report those upstream.
- Prompt-injection outcomes that do not cross a process or filesystem boundary.
