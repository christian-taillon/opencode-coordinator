"""Bounded, read-only metadata inventory. No session stores or transcript access."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import quote

from vendor_sessions import safe


VERSION = 1
PAGE_LIMIT = 100
PAGE_CAP = 5
GET_CAP = 12
REMOTE_CAP = 4
NATIVE_SECONDS = 4.0
CLAUDE_SECONDS = 2.0
HERDR_SECONDS = 3.0
SID = re.compile(r"ses_[A-Za-z0-9]+\Z")
OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PANE = re.compile(r"[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}\Z")
CALLER_HOST = socket.gethostname()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def number(value):
    return (
        value
        if type(value) in (int, float) and 0 <= value <= 9_007_199_254_740_991
        else 0
    )


def text(value, limit=120):
    return safe(value if isinstance(value, str) else "")[:limit]


def directory(value):
    # Metadata paths are display data, never traversed or passed to Git/shell.
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return text(value, 240)


def identity(source, machine, identifier):
    # Profile IDs and `local` belong to this caller's host namespace.
    scope = f"{CALLER_HOST}/{machine}"
    return ":".join(quote(v, safe="") for v in (source, scope, identifier))


def obj(value):
    return value if isinstance(value, dict) else {}


def clean_id(value, pattern=OPAQUE):
    return isinstance(value, str) and pattern.fullmatch(value) and safe(value) == value


def opaque(value):
    return clean_id(value)


def valid_native(row):
    times = obj(obj(row).get("time"))
    if "updated" not in times or any(
        type(value) not in (int, float) or value != number(value)
        for key, value in times.items()
        if key in ("created", "updated", "busy", "idle", "viewed", "archived")
    ):
        return False
    return (
        isinstance(row, dict)
        and clean_id(row.get("id"), SID)
        and isinstance(row.get("time"), dict)
        and (row.get("parentID") is None or clean_id(row["parentID"], SID))
    )


def run(argv, deadline, seconds=1.2):
    """Bound command time, output and descendants. Diagnostics never leave this boundary."""
    remaining = min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        return None, "source deadline reached"
    if not shutil.which(argv[0]):
        return None, "executable missing"
    # A pipe reader keeps output in bounded memory; kill the whole process group
    # on time/output limits, including SSH forwarding descendants.
    import selectors

    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None, "read-only command unavailable"
    end = time.monotonic() + remaining
    output = bytearray()
    error = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    error = "read-only command timed out"
                    break
                if not selector.select(left):
                    error = "read-only command timed out"
                    break
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 2_000_000:
                    error = "output exceeded 2 MB"
                    break
        if error is None:
            try:
                code = child.wait(timeout=max(0.001, end - time.monotonic()))
                if code:
                    error = "read-only command failed; diagnostics withheld"
            except subprocess.TimeoutExpired:
                error = "read-only command timed out"
    finally:
        if child.poll() is None or error:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()
        child.stdout.close()
    if error:
        return None, error
    return output.decode("utf-8", errors="replace"), None


class Source:
    def __init__(self, name, machine="local", seconds=NATIVE_SECONDS):
        self.rows = []
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + seconds
        self.coverage = {
            "source": name,
            "machine": machine,
            "status": "observed",
            "started_at": stamp(),
            "finished_at": None,
            "calls": 0,
            "records": 0,
            "truncated": False,
            "errors": [],
        }

    def error(self, reason):
        with self.lock:
            if reason not in self.coverage["errors"]:
                self.coverage["errors"].append(reason)
            self.coverage["status"] = "partial" if self.rows else "unavailable"

    def call(self, argv, seconds=1.2):
        with self.lock:
            self.coverage["calls"] += 1
        result, error = run(argv, self.deadline, seconds)
        if error:
            self.error(error)
            return None
        try:
            return json.loads(result)
        except (ValueError, TypeError, RecursionError):
            self.error("incompatible JSON; details withheld")
            return None

    def api(self, operation, **params):
        argv = ["opencode", "api", operation]
        for key, value in params.items():
            argv.extend(["--param", f"{key}={value}"])
        return self.call(argv)

    def finish(self):
        if self.coverage["errors"]:
            self.coverage["status"] = "partial" if self.rows else "unavailable"
        self.coverage.update(finished_at=stamp(), records=len(self.rows))
        return self


def base(source, machine, identifier, title, path, project=None):
    return {
        "identity": identity(source, machine, identifier),
        "source": source,
        "machine": machine,
        "id": identifier,
        "title": text(title),
        "project_id": project,
        "directory": directory(path),
        "working_directory": directory(path),
        "worktree": None,
        "execution_state": "unknown",
        "work_status": "not_assessed",
        "delivery_status": "not_assessed",
        "pending_input": "not_assessed",
        "attention": [],
        "last_activity_ms": None,
        "archived": False,
        "parent_identity": None,
        "evidence": [],
    }


def native(args, host):
    source = Source("opencode")
    if not shutil.which("opencode"):
        source.error("executable missing")
        return source.finish()
    # Independent server identity and live-execution metadata in parallel with list.
    with ThreadPoolExecutor(max_workers=3) as pool:
        info_future = pool.submit(source.api, "server.info")
        active_future = pool.submit(source.api, "session.active")
        records = {}
        cursor = None
        seen_cursors = set()
        cutoff = time.time() * 1000 - args.days * 86_400_000
        for page in range(PAGE_CAP):
            params = {"limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            else:
                params.update(archived="false", order="desc")
            packet = source.api("session.list", **params)
            if not isinstance(packet, dict) or not isinstance(packet.get("data"), list):
                source.error("session list unavailable or incompatible")
                break
            rows = packet["data"]
            oversized = len(rows) > PAGE_LIMIT
            if oversized:
                source.error("session page exceeded requested limit")
                source.coverage["truncated"] = True
                rows = rows[:PAGE_LIMIT]
            valid_rows = [row for row in rows if valid_native(row)]
            if len(valid_rows) != len(rows):
                source.error(
                    "invalid session metadata/identity omitted; IDs are never rewritten"
                )
            for row in valid_rows:
                records[row["id"]] = row
            if oversized:
                break
            next_cursor = obj(packet.get("cursor")).get("next")
            if not rows or not next_cursor:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                source.error("invalid or repeated pagination cursor")
                source.coverage["truncated"] = True
                break
            if (
                len(valid_rows) == len(rows)
                and number(rows[-1]["time"].get("updated")) < cutoff
            ):
                break
            cursor = next_cursor
            seen_cursors.add(cursor)
        else:
            source.coverage["truncated"] = True
        info = info_future.result()
        active_packet = active_future.result()
        source.coverage["host"] = (
            text(info.get("hostname")) if isinstance(info, dict) else host
        )
        active = active_packet.get("data") if isinstance(active_packet, dict) else None
        if not isinstance(active, dict) or any(
            not clean_id(k, SID)
            or not isinstance(v, dict)
            or v.get("type") != "running"
            for k, v in active.items()
        ):
            active = None
            source.error(
                "active metadata unavailable or incompatible; inactive state unknown"
            )
        missing = sorted(set(active or {}) - records.keys())
        source.coverage["active_not_fetched"] = max(0, len(missing) - GET_CAP)
        if len(missing) > GET_CAP:
            source.coverage["truncated"] = True
        futures = [
            (sid, pool.submit(source.api, "session.get", sessionID=sid))
            for sid in missing[:GET_CAP]
        ]
        for sid, future in futures:
            packet = future.result()
            row = packet.get("data") if isinstance(packet, dict) else None
            if valid_native(row) and row["id"] == sid:
                records[sid] = row
            else:
                source.error("active session metadata unavailable")
                records[sid] = {"id": sid, "time": {}, "unavailable": True}
    self_id = os.environ.get("OPENCODE_SESSION_ID", "")
    # Fetch only missing owning parents, never recursively discover a session tree.
    parent_ids = sorted(
        {
            r.get("parentID")
            for r in records.values()
            if clean_id(r.get("parentID"), SID)
            and (
                number(r["time"].get("updated")) >= cutoff or r["id"] in (active or {})
            )
        }
        - records.keys()
    )
    source.coverage["parents_not_fetched"] = max(0, len(parent_ids) - GET_CAP)
    if len(parent_ids) > GET_CAP:
        source.coverage["truncated"] = True
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            (sid, pool.submit(source.api, "session.get", sessionID=sid))
            for sid in parent_ids[:GET_CAP]
        ]
        for sid, future in futures:
            packet = future.result()
            row = packet.get("data") if isinstance(packet, dict) else None
            if valid_native(row) and row["id"] == sid:
                records[sid] = row
            else:
                source.error("owning parent metadata unavailable")
    relevant_parents = {
        r.get("parentID")
        for r in records.values()
        if r["id"] in (active or {}) or number(r["time"].get("updated")) >= cutoff
    }
    for sid, record in records.items():
        times = record["time"]
        updated = number(times.get("updated"))
        if sid == self_id or (
            updated < cutoff
            and sid not in (active or {})
            and sid not in relevant_parents
        ):
            continue
        if times.get("archived") and sid not in (active or {}):
            continue
        row = base(
            "opencode",
            "local",
            sid,
            record.get("title"),
            obj(record.get("location")).get("directory"),
            text(record.get("projectID")) or None,
        )
        row["last_activity_ms"] = (
            max(number(times.get(k)) for k in ("busy", "idle", "created")) or None
        )
        row["last_activity_basis"] = (
            "native busy/idle/created metadata; not transcript progress"
        )
        row["archived"] = bool(times.get("archived"))
        row["evidence"] = [
            "active ID only; session metadata unavailable"
            if record.get("unavailable")
            else "native session metadata; last run is not whole-work completion"
        ]
        row["agent"] = text(record.get("agent"), 40)
        subpath = record.get("subpath")
        if (
            isinstance(subpath, str)
            and row["directory"]
            and not subpath.startswith("/")
            and ".." not in subpath.split("/")
        ):
            row["working_directory"] = directory(
                row["directory"].rstrip("/") + "/" + subpath
            )
        parent = record.get("parentID")
        if clean_id(parent, SID):
            row["parent_identity"] = identity("opencode", "local", parent)
        if active is not None:
            if sid in active:
                row["execution_state"] = "running"
                row["attention"].append("running")
            elif number(times.get("busy")) > number(times.get("idle")):
                row["execution_state"] = "unowned_run"
                row["attention"].append("unowned_run")
            else:
                outcome = record.get("outcome")
                row["execution_state"] = (
                    outcome
                    if outcome in ("succeeded", "failed", "interrupted")
                    else "idle"
                )
                if outcome in ("failed", "interrupted"):
                    row["attention"].append(outcome)
            if sid not in active and number(times.get("idle")) > number(
                times.get("viewed")
            ):
                row["attention"].append("unseen_outcome")
        source.rows.append(row)
    source.coverage["pending_input"] = "not assessed; no global pending-input API"
    source.coverage["self_excluded"] = bool(self_id)
    source.coverage["scanned_records"] = len(records)
    return source.finish()


def claude():
    source = Source("claude", seconds=CLAUDE_SECONDS)
    if not shutil.which("claude"):
        source.error("executable missing")
        return source.finish()
    # This installed native contract was inspected at implementation time.
    # Normal inventory never repeats capability discovery or requests history.
    packet = source.call(["claude", "agents", "--json"], seconds=1.2)
    if not isinstance(packet, list) or any(not isinstance(r, dict) for r in packet):
        source.error("active-session metadata unavailable or incompatible")
        return source.finish()
    source.coverage["truncated"] = len(packet) > 200
    for record in packet[:200]:
        cli_id, conversation = record.get("id"), record.get("sessionId")
        if (
            record.get("kind") not in ("interactive", "background")
            or (
                record.get("state") is not None
                and (
                    not isinstance(record["state"], str) or not record["state"].strip()
                )
            )
            or any(
                value is not None and not opaque(value)
                for value in (cli_id, conversation)
            )
        ):
            source.error(
                "invalid active-session metadata/identity omitted; not agent definitions"
            )
            continue
        identifier = cli_id if opaque(cli_id) else conversation
        if not opaque(identifier):
            source.error("invalid session identity omitted")
            continue
        row = base("claude", "local", identifier, record.get("name"), record.get("cwd"))
        row.update(
            cli_id=cli_id if opaque(cli_id) else None,
            conversation_id=conversation if opaque(conversation) else None,
            frontend=record.get("kind")
            if record.get("kind") in ("interactive", "background")
            else "unknown",
            reported_state=text(record.get("state"), 24) or "unknown",
            evidence=[
                "native active-session listing; reported state may be stale; not agent definitions or history"
            ],
        )
        if row["reported_state"] in ("blocked", "failed", "working"):
            row["attention"].append(f"reported_{row['reported_state']}")
        source.rows.append(row)
    return source.finish()


def codex():
    source = Source("codex", seconds=0.8)
    if not shutil.which("codex"):
        source.error("executable missing")
    else:
        # The installed CLI has only a TUI for agents, not a bounded JSON list.
        # Capability discovery remains the explicit `sessions codex capabilities`.
        source.error(
            "no qualified bounded metadata interface; use sessions codex capabilities; no history scan"
        )
        source.coverage["status"] = "unsupported"
    source.coverage["finished_at"] = stamp()
    return source


def herdr_snapshot(machine="local", seconds=1.2):
    source = Source("herdr", machine, seconds=seconds)
    argv = (
        ["herdr"]
        + ([] if machine == "local" else ["--machine", machine])
        + ["api", "snapshot"]
    )
    packet = source.call(argv, seconds=seconds)
    result = packet.get("result") if isinstance(packet, dict) else None
    snapshot = result.get("snapshot") if isinstance(result, dict) else None
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("agents"), list)
        or not isinstance(snapshot.get("workspaces"), list)
    ):
        source.error("snapshot unavailable or incompatible; remote state not reused")
        return source.finish()
    workspaces = {
        r.get("workspace_id"): r
        for r in snapshot["workspaces"]
        if isinstance(r, dict) and isinstance(r.get("workspace_id"), str)
    }
    agents = snapshot["agents"]
    source.coverage["truncated"] = len(agents) > 200
    for record in agents[:200]:
        if not isinstance(record, dict) or not clean_id(record.get("pane_id"), PANE):
            source.error(
                "invalid pane metadata/identity omitted; IDs are never rewritten"
            )
            continue
        pane = record["pane_id"]
        workspace = workspaces.get(text(record.get("workspace_id")), {})
        row = base(
            "herdr",
            machine,
            pane,
            record.get("name") or record.get("agent"),
            record.get("foreground_cwd") or record.get("cwd"),
        )
        row["project_label"] = text(workspace.get("label"))
        worktree = workspace.get("worktree")
        if isinstance(worktree, dict):
            row["worktree"] = directory(worktree.get("checkout_path"))
        state = record.get("agent_status")
        row["reported_state"] = (
            state
            if state in ("working", "blocked", "idle", "done", "unknown")
            else "unknown"
        )
        if state == "blocked":
            row["attention"].append("blocked_ui")
            row["pending_input"] = "recognized_ui; inspect original client"
        elif state == "working":
            row["attention"].append("reported_working")
        row["evidence"] = [
            "Herdr presence/UI state only; not process execution or task completion"
        ]
        ref = record.get("agent_session")
        row["native_reference"] = None
        if (
            isinstance(ref, dict)
            and ref.get("kind") == "id"
            and ref.get("agent") in ("opencode", "claude", "codex")
        ):
            value = ref.get("value")
            if opaque(value):
                row["native_reference"] = {
                    "source": ref["agent"],
                    "id": value,
                    "machine": machine,
                }
            else:
                source.error(
                    "invalid native reference omitted; IDs are never rewritten"
                )
        # Do not retain title/tokens/last_response: those may contain agent replies.
        source.rows.append(row)
    return source.finish()


def herdr(args):
    coverage = Source("herdr", seconds=HERDR_SECONDS)
    if os.environ.get("HERDR_ENV") != "1":
        coverage.error("Herdr unavailable in this execution context.")
        return [coverage.finish()]
    if not shutil.which("herdr"):
        coverage.error("executable missing")
        return [coverage.finish()]
    if args.machine and args.machine != "local":
        return [herdr_snapshot(args.machine)]
    if not args.all_machines:
        return [herdr_snapshot()]
    # Saved profiles are not an agent inventory. Bound forwarding independently.
    with ThreadPoolExecutor(max_workers=REMOTE_CAP + 1) as pool:
        local = pool.submit(herdr_snapshot)
        packet = coverage.call(["herdr", "machine", "list", "--json"], seconds=0.8)
        if not isinstance(packet, list) or any(
            not isinstance(r, dict)
            or not isinstance(r.get("id"), str)
            or not opaque(r["id"])
            or type(r.get("enabled")) is not bool
            for r in packet
        ):
            coverage.error("saved-machine listing unavailable or incompatible")
            return [local.result(), coverage.finish()]
        profiles = sorted({r["id"] for r in packet if r["enabled"]})
        coverage.coverage["truncated"] = len(profiles) > REMOTE_CAP
        coverage.coverage["machines_omitted"] = max(0, len(profiles) - REMOTE_CAP)
        remotes = [
            pool.submit(herdr_snapshot, profile, 1.2)
            for profile in profiles[:REMOTE_CAP]
        ]
        coverage.coverage["source"] = "herdr-machines"
        return [local.result(), coverage.finish()] + [
            future.result() for future in remotes
        ]


def priority(row):
    flags = row["attention"]
    order = (
        "blocked_ui",
        "reported_blocked",
        "failed",
        "interrupted",
        "unowned_run",
        "reported_failed",
        "running",
        "reported_working",
        "unseen_outcome",
    )
    return (
        min(
            (
                order.index(f)
                for f in flags + row.get("child_attention", [])
                if f in order
            ),
            default=len(order),
        ),
        -(row["last_activity_ms"] or 0),
        row["identity"],
    )


def attention_counts(rows):
    native_rows = [r for r in rows if r["source"] == "opencode"]
    return {
        "native_running": sum(r["execution_state"] == "running" for r in native_rows),
        "native_running_children": sum(
            r["execution_state"] == "running" and bool(r["parent_identity"])
            for r in native_rows
        ),
        "native_failed_or_interrupted": sum(
            r["execution_state"] in ("failed", "interrupted") for r in native_rows
        ),
        "native_unowned_runs": sum(
            r["execution_state"] == "unowned_run" for r in native_rows
        ),
        "native_unseen_outcomes": sum(
            "unseen_outcome" in r["attention"] for r in native_rows
        ),
        "herdr_blocked_ui": sum(
            r["source"] == "herdr" and r.get("reported_state") == "blocked"
            for r in rows
        ),
        "herdr_reported_working": sum(
            r["source"] == "herdr" and r.get("reported_state") == "working"
            for r in rows
        ),
        "claude_reported_blocked": sum(
            r["source"] == "claude" and r.get("reported_state") == "blocked"
            for r in rows
        ),
        "claude_reported_working": sum(
            r["source"] == "claude" and r.get("reported_state") == "working"
            for r in rows
        ),
        "claude_reported_failed": sum(
            r["source"] == "claude" and r.get("reported_state") == "failed"
            for r in rows
        ),
    }


def collect(args, native_only=False):
    started, began = stamp(), time.monotonic()
    host = CALLER_HOST
    if safe(host) != host or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host):
        gap = Source("identity")
        gap.error(
            "unsafe caller host namespace; no identities emitted or sources queried"
        )
        sources = [gap.finish()]
        host = "unavailable"
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            native_future = (
                pool.submit(native, args, host)
                if args.machine in (None, "local")
                else None
            )
            claude_future = (
                pool.submit(claude)
                if not native_only and args.machine in (None, "local")
                else None
            )
            codex_future = (
                pool.submit(codex)
                if not native_only and args.machine in (None, "local")
                else None
            )
            herdr_future = pool.submit(herdr, args) if not native_only else None
            sources = [
                f.result() for f in (native_future, claude_future, codex_future) if f
            ]
            if herdr_future:
                sources += herdr_future.result()
    all_rows = [r for source in sources for r in source.rows]
    # Join only source-qualified native IDs on the same machine. A cwd match is
    # never identity evidence, including two sessions in the same checkout.
    native_rows = {r["identity"]: r for r in all_rows if r["source"] != "herdr"}
    claude_conversations = {
        identity("claude", r["machine"], r["conversation_id"]): r
        for r in all_rows
        if r["source"] == "claude" and r.get("conversation_id")
    }
    for row in all_rows:
        ref = row.get("native_reference")
        if ref:
            key = identity(ref["source"], ref["machine"], ref["id"])
            match = native_rows.get(key) or claude_conversations.get(key)
            row["correlated_identity"] = match["identity"] if match else None
        else:
            row["correlated_identity"] = None
    matching = [
        r
        for r in all_rows
        if not args.project
        or args.project.casefold()
        in " ".join(
            str(r.get(k) or "")
            for k in (
                "project_id",
                "project_label",
                "directory",
                "working_directory",
                "worktree",
            )
        ).casefold()
    ]
    by_id = {r["identity"]: r for r in matching}
    roots = []
    for row in matching:
        parent = by_id.get(row["parent_identity"])
        visited = {row["identity"]}
        while (
            parent
            and parent["parent_identity"] in by_id
            and parent["identity"] not in visited
        ):
            visited.add(parent["identity"])
            parent = by_id[parent["parent_identity"]]
        if parent and parent["identity"] in visited:
            row["evidence"].append("cyclic parent metadata; displayed independently")
            parent = None
        if parent:
            parent.setdefault("children", []).append(row)
        else:
            roots.append(row)
    for row in roots:
        row.setdefault("children", [])
        row["children"].sort(key=priority)
        row["child_attention"] = sorted(
            {f for c in row["children"] for f in c["attention"]}
        )
    roots.sort(key=priority)
    offset = getattr(args, "offset", 0)
    selected = roots[offset : offset + args.max]
    groups = defaultdict(list)
    for row in selected:
        # Native project IDs retain distinct projects with identical basenames.
        key = (
            row["machine"],
            row["source"],
            row["project_id"] or row["directory"] or "unknown",
        )
        groups[key].append(row)
    packet = {
        "schema_version": VERSION,
        "started_at": started,
        "as_of": stamp(),
        "host": host,
        "elapsed_ms": round((time.monotonic() - began) * 1000),
        "scope": {
            "days": args.days,
            "max": args.max,
            "project": text(args.project),
            "machine": args.machine or "local",
            "all_machines": args.all_machines,
        },
        "coverage": [s.coverage for s in sources],
        "partial_failure": any(
            s.coverage["status"] in ("partial", "unavailable") for s in sources
        ),
        "truncated": len(roots) > len(selected)
        or any(s.coverage["truncated"] for s in sources),
        "counts": {
            "records": len(matching),
            "roots": len(roots),
            "shown": len(selected),
            "remaining": max(0, len(roots) - offset - len(selected)),
            "children": sum(len(r.get("children", [])) for r in roots),
        },
        "attention_counts": attention_counts(matching),
        "unresolved_parent_records": sum(bool(r["parent_identity"]) for r in roots),
        "identity_scope": "Caller-host/profile-qualified. Presence and native records remain separate; counts are not unique agents.",
        "not_assessed": [
            "work completion",
            "delivery",
            "per-session inbox/permissions/forms",
            "Git/GitHub",
        ],
        "groups": [
            {
                "machine": k[0],
                "source": k[1],
                "project": k[2],
                "directory": v[0]["directory"],
                "sessions": v,
            }
            for k, v in groups.items()
        ],
    }
    return packet


def cleanup(args):
    began = time.monotonic()
    packet = collect(args, native_only=True)
    source = Source("opencode", seconds=4.0)
    rows = [
        r for g in packet["groups"] for r in g["sessions"] if r["source"] == "opencode"
    ]
    # At most 20 roots and eight known children per root, explicit bounded batch.
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = []
        for row in rows:
            targets = [row] + row["children"][:8]
            for target in targets:
                futures = {
                    op: pool.submit(
                        source.api, f"session.{op}.list", sessionID=target["id"]
                    )
                    for op in ("inbox", "permission", "form")
                }
                checks.append((row, target, futures))
        for root, row, futures in checks:
            counts = {}
            for op, future in futures.items():
                result = future.result()
                counts[op] = (
                    len(result["data"])
                    if isinstance(result, dict) and isinstance(result.get("data"), list)
                    else None
                )
                if counts[op] is None:
                    source.error("pending-input check incomplete; unknown, not empty")
            row["pending_counts"] = counts
            row["pending_input"] = (
                "present"
                if any(counts.values())
                else "unknown"
                if None in counts.values()
                else "empty_at_snapshot"
            )
    for row in rows:
        targets = [row] + row["children"][:8]
        if any(
            r["pending_input"] == "present" or r["execution_state"] == "running"
            for r in targets
        ):
            row["cleanup_status"] = "not eligible; running or pending input"
        elif (
            len(row["children"]) > 8
            or packet["truncated"]
            or any(r["pending_input"] == "unknown" for r in targets)
        ):
            row["cleanup_status"] = "unknown; coverage incomplete"
        else:
            row["cleanup_status"] = (
                "needs targeted review; completion/delivery/other children not assessed"
            )
    source.coverage.update(
        source="opencode-pending",
        inspected_sessions=len(checks),
        inbox_contract="OpenCode v2.2.6 schema: durable enqueued work not yet delivered; inbox list counts are pending only.",
    )
    packet["coverage"].append(source.finish().coverage)
    packet["partial_failure"] |= bool(source.coverage["errors"])
    packet["operation"] = "cleanup-plan"
    packet["next_offset"] = (
        args.offset + packet["counts"]["shown"]
        if packet["counts"]["remaining"]
        else None
    )
    packet["archive_authorized"] = False
    packet["archive_scope"] = (
        "Archive hides records only; no pane close, process stop, worktree or branch removal. Confirm exact IDs after targeted review."
    )
    packet["as_of"] = stamp()
    packet["elapsed_ms"] = round((time.monotonic() - began) * 1000)
    packet["not_assessed"] = [
        "work completion",
        "delivery",
        "inputs outside selected batch",
        "Git/GitHub",
    ]
    return packet


def activity_age(row, now_ms):
    then = row["last_activity_ms"]
    if then is None:
        return "?"
    seconds = max(0, (now_ms - then) / 1000)
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86_400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86_400)}d"


def row_status(row):
    if row["source"] != "opencode":
        reported = text(row.get("reported_state"), 24) or "unknown"
        action = (
            "inspect Claude client"
            if row["source"] == "claude"
            else (
                "inspect/answer Herdr UI"
                if reported == "blocked"
                else "inspect Herdr client"
            )
        )
        return f"reported {reported}", action
    state = row["execution_state"]
    flags = row["attention"]
    status = f"run {state}" + (", unseen" if "unseen_outcome" in flags else "")
    if state in ("failed", "interrupted", "unowned_run", "unknown"):
        action = "show/tail"
    elif row.get("child_attention"):
        action = "show children"
    elif state == "running":
        action = "leave running; show if needed"
    elif "unseen_outcome" in flags:
        action = "read outcome (tail)"
    else:
        action = "inspect if needed"
    if row["children"]:
        child_counts = [
            (flag, sum(flag in child["attention"] for child in row["children"]))
            for flag in (
                "running",
                "failed",
                "interrupted",
                "unowned_run",
                "unseen_outcome",
            )
        ]
        evidence = ",".join(
            f"{count} {flag.replace('_outcome', '')}"
            for flag, count in child_counts
            if count
        )
        status += f"; kids {evidence or str(len(row['children']))}"
    return status, action


def human(packet):
    c = packet["counts"]
    a = packet["attention_counts"]
    lines = [
        f"Sessions · {packet['as_of']} · {c['roots']} top-level metadata/presence records / {c['children']} children · selected {c['shown']} (not unique agents)",
        f"Matched evidence: native running {a['native_running']} ({a['native_running_children']} children), failed/interrupted {a['native_failed_or_interrupted']}, unowned {a['native_unowned_runs']}, unseen {a['native_unseen_outcomes']}; Herdr blocked UI {a['herdr_blocked_ui']}, reported working {a['herdr_reported_working']}; Claude reported blocked {a['claude_reported_blocked']}, working {a['claude_reported_working']}, failed {a['claude_reported_failed']}.",
        "Work/delivery not assessed. Native pending input unknown; vendor states reported, execution unknown. Last-active is metadata age, ? unknown.",
    ]
    now_ms = datetime.fromisoformat(packet["as_of"]).timestamp() * 1000
    footer = []
    for source in packet["coverage"]:
        detail = "; ".join(source["errors"]) or f"{source['records']} metadata records"
        cap = "; capped" if source["truncated"] else ""
        footer.append(
            f"{source['source']}@{text(source['machine'], 40)}: {source['status']} ({text(detail, 130)}{cap})"
        )
    footer.append(
        "Exact IDs: overview --json --project TEXT. Inspect: show ID / tail ID; project review is explicit."
    )

    # Conservative display estimate, not a model-specific tokenizer. Reserve
    # coverage and the omitted count even for high-token-density Unicode titles.
    def cost(line):
        return (sum(1 if ord(c) < 128 else 12 for c in line) + 2) // 3

    tokens = 1300 - sum(cost(line) for line in lines + footer)
    budget = 24
    displayed = 0
    for group in packet["groups"]:
        if budget <= 0 or tokens <= 0:
            break
        label = text(group["directory"] or group["project"], 60)
        machine = packet["host"] if group["machine"] == "local" else group["machine"]
        heading = f"{text(machine, 40)} / {group['source']} / {label}"
        if cost(heading) > tokens:
            break
        lines.append(heading)
        tokens -= cost(heading)
        budget -= 1
        attention_rows = [
            r for r in group["sessions"] if r["attention"] or r.get("child_attention")
        ]
        # Do not let quiet siblings hide another project's blocker or vendor.
        group_rows = attention_rows[:3] if attention_rows else group["sessions"][:1]
        for row in group_rows:
            if budget <= 0:
                break
            status, action = row_status(row)
            title = text(row["title"], 40) or "(untitled)"
            line = f"  {title} — last-active {activity_age(row, now_ms)}; {status} → {action}"
            if cost(line) > tokens:
                budget = 0
                break
            lines.append(line)
            tokens -= cost(line)
            displayed += 1
            budget -= 1
    omitted = c["remaining"] + max(0, c["shown"] - displayed)
    if omitted:
        lines.append(
            f"{omitted} more records not displayed; narrow --project or use --json."
        )
    return "\n".join(lines + footer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("overview", "cleanup-plan"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--project", default="")
    machine = parser.add_mutually_exclusive_group()
    machine.add_argument("--machine")
    machine.add_argument("--all-machines", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.days <= 90 or not 1 <= args.max <= 100 or args.offset < 0:
        parser.error("--days 1-90; --max 1-100; --offset nonnegative")
    if args.machine and (not opaque(args.machine) or args.machine.startswith("-")):
        parser.error("--machine requires a saved profile ID/label or local")
    if args.operation == "cleanup-plan" and (
        not args.project
        or args.max > 20
        or args.all_machines
        or args.machine not in (None, "local")
    ):
        parser.error(
            "cleanup-plan requires --project and --max 1-20, local OpenCode only"
        )
    packet = cleanup(args) if args.operation == "cleanup-plan" else collect(args)
    print(
        json.dumps(packet, ensure_ascii=True, indent=2)
        if args.json or args.operation == "cleanup-plan"
        else human(packet)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
