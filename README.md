# OpenCode Session Coordinator

See what needs attention across projects, then inspect or continue the owning
session only when requested.

The coordinator finds the session that owns the work. That session does the
editing, testing, and Git work. This bundle does not add a service, database,
or background monitor.

Built for OpenCode v2.2.5–2.2.6 on Linux. No daemon, database or task tracker.

## Install

You need Bash, Python 3.11+, jq and GNU coreutils. The CLI inventory requires no
model or inference; agent-driven investigation needs an available model.

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
~/.config/opencode/skills/session-coordination/scripts/sessions overview
```

In OpenCode, `/session-status` invokes that fast path without loading the large
investigation skill. Choose the `opencode` agent only when you want coordination.
Installation/activation is separate from repository development; do not restart
the managed service or overwrite an existing installation automatically.

### Update an existing installation

Do not run `install.sh` over an existing copy. After review, publication and
explicit install approval, run this **from the repository in Bash**. It stages
the exact approved commit, backs up only replaced bundle files outside the
repository, and replaces each file atomically. It does not change the checkout,
custom agent, model, permissions or `opencode.json`, and does not reload services.

<!-- existing-install-update -->
```bash
set -euo pipefail
umask 077
repo=$PWD
dest="$HOME/.config/opencode"
read -r -p 'Approved full commit SHA: ' revision
[[ $revision =~ ^[0-9a-f]{40}$ ]]
git -C "$repo" fetch origin "$revision"
test "$(git -C "$repo" rev-parse FETCH_HEAD)" = "$revision"
files=(
  skills/session-coordination/scripts/vendor_sessions.py
  skills/session-coordination/scripts/inventory.py
  skills/session-coordination/scripts/sessions
  skills/session-coordination/SKILL.md
  commands/session-status.md
  commands/session-review.md
  commands/session-act.md
)
scratch=${TMPDIR:-/tmp}
if test -d /tmp/opencode; then
  scratch=/tmp/opencode
fi
stage=$(mktemp -d "$scratch/coordinator-install.XXXXXX")
backup=$(mktemp -d "$HOME/.config/opencode-coordinator-backup.XXXXXX")
git -C "$repo" archive "$revision" -- "${files[@]}" | tar -x -C "$stage"
for path in "${files[@]}"; do
  test -f "$stage/$path"
  test ! -L "$dest/$path"
done
for path in "${files[@]}"; do
  if test -e "$dest/$path"; then
    mkdir -p "$backup/$(dirname "$path")"
    cp -p -- "$dest/$path" "$backup/$path"
  fi
done
for path in "${files[@]}"; do
  directory="$dest/$(dirname "$path")"
  mkdir -p "$directory"
  temporary=$(mktemp "$directory/.coordinator-install.XXXXXX")
  mode=644
  test "$path" != skills/session-coordination/scripts/sessions || mode=755
  install -m "$mode" "$stage/$path" "$temporary"
  mv -- "$temporary" "$dest/$path"
done
printf 'Installed %s; backup: %s\n' "$revision" "$backup"
```

Then run the installed `sessions overview` path above and `/session-status` in
OpenCode; no service reload. Before installation, only the repository executable
`./skills/session-coordination/scripts/sessions overview` has the new behavior.
The update deliberately leaves `agents/opencode.md` untouched. Default attention
routing needs a separately reviewed **body-only** merge from the repository's
agent instructions; preserve the complete installed YAML frontmatter (including
model and ordered permissions) and any other custom instructions.

To remove the bundle, delete only:

```text
~/.config/opencode/agents/opencode.md
~/.config/opencode/skills/session-coordination/
~/.config/opencode/commands/session-review.md
~/.config/opencode/commands/session-act.md
~/.config/opencode/commands/session-status.md
```

## Use

| Goal | Command |
| --- | --- |
| Quick inventory | `/session-status` |
| Narrow inventory | `/session-status --project Example --days 14` |
| Inspect one session | Helper: `show ID`, then `tail ID --turns 1` if needed |
| Explicit project review | `/session-review Example API over the last week` |
| Plan a cleanup batch | Helper: `cleanup-plan --project Example --max 10` |
| Confirmed archive only | Helper: `archive EXACT_ID…`; `restore EXACT_ID…` reverses it |
| Ask a session for status | `/session-act Ask SESSION_ID for a read-only status. Do not edit or publish.` |
| Continue owned work | `/session-act Continue SESSION_ID. Commit allowed; do not push or merge.` |

“Helper” means `~/.config/opencode/skills/session-coordination/scripts/sessions`,
not a shell alias. `/session-status` and `/session-review` only read.
`/session-act` asks when the target, outcome, or
authority is missing. A suggestion is not permission to publish, merge, delete,
or archive.

Answer pending OpenCode permission prompts in the client. Do not turn them into
another instruction.

## Quick inventory versus investigation

`overview [--json] [--days 14] [--max 20] [--project TEXT]` reads bounded recent
OpenCode metadata plus active work. It groups by host/project and flags native
running, failed/interrupted, unowned-run and unseen-outcome evidence. It never
reads transcripts, checks every root's input, traverses directories, calls Git/
GitHub, or invokes inference. Work and delivery are **not assessed**. Native
pending input is unknown until targeted inspection; unseen is not an open task.

JSON schema version 1 includes exact source/host-qualified identities, timestamps,
coverage, limits and partial failures. Text hides ID lists and stays compact.
Attention totals count matched metadata/presence, not unique agents. Text gives
last-active metadata age (`?` when unknown), reported vendor state, child evidence
separately from root execution, and a source-appropriate inspection action.
Text shows at most three attention rows per group (one quiet row otherwise);
JSON retains the selected rows and exact IDs without this display collapse.
Limits: five 100-record pages, at most 12 missing active records and 12 parents;
four-second native deadline. No persistent cache or inventory state is written.
Snapshots are non-atomic and bounded, not a complete historical audit.

Optional Herdr presence uses one-shot CLI snapshots only inside an authorized
`HERDR_ENV=1` caller. Otherwise: **Herdr unavailable in this execution context.**
`--machine PROFILE_ID` selects a saved machine; `--all-machines` snapshots local
plus at most four enabled saved profiles concurrently (1.2-second timeouts).
No socket probing, reconnect, server start or cached remote truth. Herdr's
recognized blocked UI is not native permission/form evidence. Explicit session
references can correlate on the same machine; matching directories cannot.

Explicit `show`, `tail`, `review` and `delivery` add pending inputs, bounded
replies, context estimates and attributed Git/remote evidence only as requested.
See the [lazy skill](skills/session-coordination/SKILL.md) for procedures. A
successful run is not whole-task completion; missing evidence stays unknown.

`cleanup-plan --project TEXT --max 10 [--offset N]` checks pending-input counts
for a bounded native batch and up to eight known children per root. It reports
remaining coverage; errors are unknown, never empty. Completion, other children
and delivery need targeted review before proposing exact IDs for confirmation.
The inspected OpenCode v2.2.6 schema defines inbox entries as enqueued work **not
yet delivered**, so inbox length counts pending items, not consumed history.
No automatic archive. Archive does not close a pane, stop a process, or remove
a worktree/branch. Historical audit counts are not current truth.

## Vendor discovery limits

Overview calls the previously inspected native `claude agents --json` contract
directly, with **no repeated capability probing**, `--all` or history scan.
It validates active interactive/background session shape, rejecting definitions.
Its source deadline is two seconds. Deeper explicit
`claude review` / `claude show EXACT_ID` retain the existing metadata-only
`--all` listing; `codex capabilities` retains explicit capability discovery.

Claude review is **metadata only**: interactive/background identity, separate
CLI/conversation IDs, reported state, as-of UTC, and explicit unknowns. It does
not read transcripts or establish liveness, completion, pending approvals,
context use, or quota. Codex reports installed CLI capabilities only; an
advertised queue command is not bundle support or runtime qualification. The
inspected Codex `agents` interface is a TUI, not a bounded JSON list; overview
reports the gap and performs no Codex discovery/history scan.

Existing-session messaging is currently unsupported. `/session-act` returns a
client handoff for vendor targets; it never substitutes a worker or resume call.
`sessions claude send ID` returns unsupported without sending or reading stdin.
Bounded delegation belongs to Switchboard, not another execution adapter here.

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
uv run tests/test_inventory.py
bash -n install.sh skills/session-coordination/scripts/sessions
shellcheck -S warning install.sh skills/session-coordination/scripts/sessions
git diff --check
```

Tests use a fake OpenCode API and disposable local Git repositories. They do
not call a live server or change real sessions.

## License

[MIT](LICENSE) © 2026 Christian Taillon
