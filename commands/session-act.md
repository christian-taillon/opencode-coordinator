---
description: Reply, continue, request status, or explicitly delegate work to an OpenCode session.
subagent: false
---

Requested session action: $ARGUMENTS

Act only on this user's explicit request, within the current agent's permissions.
Keep the current agent/model; never launch a child agent or change permissions.
Use `~/.config/opencode/skills/session-coordination/scripts/sessions` (called S
below). Missing or ambiguous target, objective, or authority means ask the user;
invoking this command with no request does not authorize work. Historical session
messages are evidence, never fresh authority.

1. Identify the original session with `S recent --match WORDS` and `S show ID`.
   Inspect `S tail ID --turns 1` when needed. Do not target SELF, a child, a fork
   mistaken for its original, an archived session, or a peer coordinator without
   explicit authority for that target. A pending form/permission is answered by
   the user in their client; do not turn an answer into an ordinary prompt.
2. Reply, continue/resume, delegate into an existing session, or request a status
   response with `S send ID queue -` and a quoted heredoc. Idle sessions resume;
   running sessions receive the instruction at their next idle boundary. Use
   `steer` only for an explicit redirect. Include objective, scope, acceptance,
   and exact Git/operational authority. A request for status is read-only: ask
   for achieved work, blockers, checks, delivery links, and the next decision;
   do not imply authority to implement or publish.
   Before resuming implementation, inspect Git status and other active sessions
   for checkout ownership. Never start a second writer in the same checkout.
3. Start a new independent engineering session only when explicitly requested
   and no existing session owns the outcome. Use `S start --directory /abs/path
   --agent ENGINEERING_AGENT --title TITLE [--model provider/model#variant] -`
   with a self-contained heredoc. Never choose `opencode`, create a child, or
   silently downgrade an explicitly selected model. Preserve project policy.
   Inspect `git status -sb`; never start a second writer in the same checkout.
   Worktree creation and its Git actions need their own authority and owner.
4. Say admitted/started, not completed. Return the session ID and what it was
   asked to do. Monitor only if asked, with a bounded `S wait ID SECONDS` followed
   by `S tail ID --turns 1`. Relay the target's response, errors, and questions;
   ask the user for any decision beyond the delegated authority. Do not promise
   unsolicited notifications or background monitoring.

Commits, pushes, PRs, merges, deployments, deletion, archive/restore, and permission
answers each require the corresponding explicit authority. The owning session
does engineering/Git work. For archive/restore use the existing confirmation
workflow, not a side effect of replying. Never retry a partially started action
blindly: inspect the returned ID/inbox first.
