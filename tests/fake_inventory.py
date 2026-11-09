#!/usr/bin/env python3
"""Read-only synthetic CLI sources, with atomic call logging for concurrency tests."""

import json
import os
from pathlib import Path
import sys
import time


vendor, *args = sys.argv[1:]
data = json.loads(Path(os.environ["INVENTORY_FIXTURE"]).read_text())
key = f"{vendor} {' '.join(args)}"
fd = os.open(
    os.environ["INVENTORY_CALLS"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
)
os.write(fd, (json.dumps([vendor, args, time.monotonic()]) + "\n").encode())
os.close(fd)
time.sleep(data.get("delays", {}).get(vendor, 0))
if any(key.startswith(prefix) for prefix in data.get("fail", [])):
    sys.exit("token=SECRET_DIAGNOSTIC")
if key in data.get("raw", {}):
    print(data["raw"][key])
    sys.exit(0)
if vendor == "opencode":
    assert args.pop(0) == "api"
    op, *options = args
    params = dict(options[i + 1].split("=", 1) for i in range(0, len(options), 2))
    assert all(options[i] == "--param" for i in range(0, len(options), 2))
    if op == "server.info":
        result = {"hostname": "fixture-local", "version": "2.2.6"}
    elif op == "session.active":
        result = {"data": data.get("active", {})}
    elif op == "session.list":
        cursor = params.get("cursor")
        if cursor:
            assert set(params) == {"limit", "cursor"}
        else:
            assert params == {"limit": "100", "archived": "false", "order": "desc"}
        rows = sorted(
            (r for r in data["sessions"] if not r["time"].get("archived")),
            key=lambda r: -r["time"]["updated"],
        )
        start = int(cursor or 0)
        end = start + data.get("page_size", 100)
        page = rows[start:end]
        result = {"data": page, "cursor": {"next": str(end)} if page else {}}
    elif op == "session.get":
        result = {
            "data": next(r for r in data["sessions"] if r["id"] == params["sessionID"])
        }
    elif op in ("session.inbox.list", "session.permission.list", "session.form.list"):
        result = {"data": data.get(op, {}).get(params["sessionID"], [])}
    else:
        sys.exit("UNEXPECTED_OR_MUTATING_OPERATION")
elif vendor == "claude":
    assert args == ["agents", "--json"], (
        "No capability discovery, history, --all, model call, or interactive invocation"
    )
    result = data.get("claude", [])
elif vendor == "herdr":
    if args == ["machine", "list", "--json"]:
        result = data.get("machines", [])
    else:
        machine = "local"
        if args[:1] == ["--machine"]:
            _, machine, *args = args
        assert args == ["api", "snapshot"], (
            "No socket, control, or transcript operation"
        )
        if machine in data.get("unreachable", []):
            time.sleep(10)
        result = data.get("snapshots", {}).get(
            machine, {"result": {"snapshot": {"agents": [], "workspaces": []}}}
        )
else:
    sys.exit("UNEXPECTED_VENDOR_EXECUTION")
print(json.dumps(result))
