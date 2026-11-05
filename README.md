# OpenCode Session Coordinator

Review and continue OpenCode sessions across every project on one server.

The coordinator finds the session that owns the work. That session does the
editing, testing, and Git work. This bundle does not add a service, database,
or background monitor.

Built for OpenCode v2.2.5 on Linux.

## Install

You need Bash, jq, GNU coreutils, and an authenticated OpenCode model.

Review `agents/opencode.md` first. Its default model is
`openai/gpt-6-luna#medium`; change that if your provider does not offer it.
Installation does not change your default agent, `opencode.json`, or existing
permissions.

```sh
./install.sh
```

The script copies the bundle into `~/.config/opencode` and stops if any
destination already exists. OpenCode discovers the new files without a server
restart. Do not start a second OpenCode server.

```sh
opencode run --agent opencode "What is running, and what needs me?"
```

To update, compare the files, back them up, and replace them. Do not run
`install.sh` over an existing copy. To remove the bundle, delete only:

```text
~/.config/opencode/agents/opencode.md
~/.config/opencode/skills/session-coordination/
~/.config/opencode/commands/session-review.md
~/.config/opencode/commands/session-act.md
```

## Use

| Goal | Command |
| --- | --- |
| See what needs you | `/session-review` |
| Narrow the review | `/session-review Example API over the last week` |
| Ask a session for status | `/session-act Ask SESSION_ID for a read-only status. Do not edit or publish.` |
| Continue owned work | `/session-act Continue SESSION_ID. Commit allowed; do not push or merge.` |

`/session-review` only reads. `/session-act` asks when the target, outcome, or
authority is missing. A suggestion is not permission to publish, merge, delete,
or archive.

Answer pending OpenCode permission prompts in the client. Do not turn them into
another instruction.

## What a review shows

- When the session last made progress, in your local timezone
- Whether it is running, idle, failed, or interrupted
- The selected model and the model used by the measured request
- Estimated context use against that model's input limit
- Whether the task is awaiting you, unfinished, reported complete, verified as
  delivered, or unknown
- Commit, push, pull request, and merge evidence when a session identifies the
  exact branch and commit
- Whether the named branch and worktree are currently present
- One next action, with the session ID to use

A successful run is not proof that the task is finished. Missing evidence stays
unknown.

## Safety

- The coordinator cannot edit files or launch child agents.
- Commits, pushes, pull requests, merges, and cleanup each need explicit
  authority. The owning session performs them.
- Two sessions must not write to the same checkout.
- Redaction catches common credential shapes. It is not a secret scanner, so do
  not publish raw session output.
- Shell permission is not an operating-system sandbox. Keep your existing deny
  rules for force-push, reset, and sudo.

## Develop

```sh
uv run tests/test_config.py
bash -n install.sh skills/session-coordination/scripts/sessions
shellcheck -S warning install.sh skills/session-coordination/scripts/sessions
```

Tests use a fake OpenCode API and disposable local Git repositories. They do
not call a live server or change real sessions.

## License

[MIT](LICENSE) © 2026 Christian Taillon
