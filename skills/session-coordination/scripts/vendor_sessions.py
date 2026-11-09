"""Read-only vendor CLI boundary. No prompts, resume, sockets, or transcript store."""

import argparse
from datetime import datetime, timezone
import json
import re
import shutil
import subprocess
import tempfile


def probe(vendor, *args):
    """Bound time and retained output; never relay vendor diagnostics."""
    if not shutil.which(vendor):
        return None, "executable missing"
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(
                [vendor, *args],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "read-only probe timed out"
        except OSError:
            return None, "read-only probe unavailable"
        if result.returncode:
            return None, "read-only probe failed; diagnostics withheld"
        if out.tell() > 2_000_000:
            return None, "read-only output exceeded 2 MB; not qualified"
        out.seek(0)
        return out.read().decode("utf-8", errors="replace"), None


def safe(value):
    # Metadata is untrusted too. Do not emit unknown fields or raw diagnostics.
    value = str(value) if value is not None else ""
    value = re.sub(
        r"(?i)(api[_-]?key|secret|token|password|pwd)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        value,
    )
    value = re.sub(r"(?i)(bearer|basic)\s+\S+", r"\1 <redacted>", value)
    value = re.sub(r"://[^/\s@]+@", "://<redacted>@", value)
    value = re.sub(
        r"sk-[\w-]{16,}|gh[pousr]_[\w]{20,}|github_pat_[\w]{20,}", "<redacted>", value
    )
    return " ".join(value.split())[:240]


def capabilities(vendor):
    version, version_error = probe(vendor, "--version")
    help_text, error = probe(vendor, "agents", "--help")
    available = help_text is not None
    data = {
        "version": safe(version) if version else None,
        "evidence": "installed CLI help only; not authentication or runtime qualification",
        "probe_error": error or version_error,
        "discovery": "client-only",
        "message": "unsupported by this bundle",
        "resume": "client-only; never invoked",
    }
    if vendor == "claude":
        supported = available and all(
            word in help_text
            for word in (
                "--json",
                "--all",
                "interactive and",
                "background",
                "JSON array",
            )
        )
        data["discovery"] = (
            "claude agents --json --all" if supported else "unverified; client-only"
        )
        data["message_reason"] = (
            "No verified existing-session messaging transport; resume can launch work or copy a running session"
        )
        return data, supported
    queue_help, queue_error = probe(vendor, "queue", "--help")
    data["queue_advertised"] = bool(
        queue_help and "--thread" in queue_help and "--message" in queue_help
    )
    data["queue_probe_error"] = queue_error
    data["message_reason"] = (
        "Discovery-only integration; advertised queue is not an implemented or runtime-qualified action"
    )
    return data, False


def review(rows, maximum, target):
    def identifier(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)

    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not (identifier(row.get("id")) or identifier(row.get("sessionId")))
        for row in rows
    ):
        raise ValueError("expected JSON array of session objects")
    selected = [
        row
        for row in rows
        if target is None or row.get("id") == target or row.get("sessionId") == target
    ]
    result = []
    for row in selected[:maximum]:
        # Observed fields in Claude 2.1.319. They are metadata, not liveness proof.
        kind = row.get("kind")
        result.append(
            {
                "id": row.get("id") if identifier(row.get("id")) else None,
                "conversation_id": row.get("sessionId")
                if identifier(row.get("sessionId"))
                else None,
                "name": safe(row.get("name")),
                "directory": safe(row.get("cwd")),
                "frontend": kind
                if kind in ("interactive", "background")
                else "unknown",
                "state_reported": safe(row.get("state")) or "unknown",
                "started_at_reported": row.get("startedAt")
                if type(row.get("startedAt")) is int
                else None,
                "evidence": "CLI discovery metadata; may be stale; not independently verified",
                "execution": "unknown",
                "work_status": "unknown",
                "permissions": None,
                "forms": None,
                "tool_denials": None,
                "inbox": {"accepted": None, "read": None, "executed": None},
                "context": {"estimate": None, "capacity": None, "percent": None},
                "model": None,
                "usage": None,
                "reply": None,
                "next_action": "Inspect the exact session in the Claude client; answer structured input there",
            }
        )
    return result, len(selected) - len(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vendor", choices=("claude", "codex"))
    parser.add_argument(
        "operation", choices=("capabilities", "recent", "review", "show", "send")
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("--max", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.max <= 100:
        parser.error("--max must be 1-100")
    if args.operation == "show" and not args.target:
        parser.error("show requires an exact discovery ID or conversation ID")
    # No prompt argument or stdin is ever consumed, even for unsupported sends.
    packet = {
        "vendor": args.vendor,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "operation": args.operation,
        "status": "unsupported",
        "coverage": "CLI metadata only; no transcripts, approvals, or independent liveness check",
        "client_handoff": (
            "Use claude agents to inspect; claude attach BACKGROUND_ID opens a background session. "
            "For interactive sessions return to the original terminal. Verify conversation_id separately; "
            "do not resume a live session or copy it. Answer pending input in that client."
            if args.vendor == "claude"
            else "Use codex agents in the client to inspect the existing session and pending input. "
            "No queue/resume performed; do not retry or start a replacement automatically."
        ),
    }
    if args.operation == "send":
        packet["reason"] = (
            "No verified reusable existing-session messaging transport in this bundle; no message admitted"
        )
        packet["effects"] = "none"
        print(json.dumps(packet, indent=2))
        return 2
    caps, discovery = capabilities(args.vendor)
    packet["capabilities"] = caps
    if args.operation == "capabilities":
        packet["status"] = "discovered" if caps["version"] else "unavailable"
    elif args.vendor == "claude" and discovery:
        raw, error = probe("claude", "agents", "--json", "--all")
        try:
            if error:
                raise ValueError(error)
            rows, omitted = review(json.loads(raw), args.max, args.target)
            packet.update(status="observed", sessions=rows, omitted=omitted)
            if args.target and not rows:
                packet.update(
                    status="not-found",
                    reason="Not in discovery; absence is not proof of deletion or completion",
                )
        except (ValueError, TypeError):
            packet.update(
                status="unavailable",
                reason="Discovery failed or incompatible JSON; details withheld",
            )
    print(json.dumps(packet, indent=2))
    return 0 if packet["status"] in ("observed", "discovered") else 2


if __name__ == "__main__":
    raise SystemExit(main())
