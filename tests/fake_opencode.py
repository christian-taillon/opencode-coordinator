#!/usr/bin/env python3
"""Synthetic API fixture for the bundle tests. Never contacts a real server."""

import json
import os
from pathlib import Path
import sys


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "api":
        sys.exit("fixture accepts only native API operations")
    operation, params, body = args[1], {}, {}
    for index in range(2, len(args), 2):
        flag, value = args[index : index + 2]
        if flag == "--param":
            key, value = value.split("=", 1)
            params[key] = value
        elif flag == "--data":
            body = json.loads(value)
        else:
            sys.exit(f"unexpected fixture flag: {flag}")
    state_path = Path(os.environ["COORD_TEST_STATE"])
    state = json.loads(state_path.read_text())
    state["calls"].append([operation, params, body])
    events = state.get("operation_events", {}).get(operation, [])
    if events:
        state.update(events.pop(0))
    session_id = params.get("sessionID")
    sessions = state["sessions"]

    if operation in state.get("fail_operations", []):
        state_path.write_text(json.dumps(state))
        print("token=fixture-failure-secret", file=sys.stderr)
        sys.exit(1)
    if operation == "server.info":
        result = {"hostname": "fixture-host", "version": "2.2.5", "pid": 1}
    elif operation == "session.active":
        result = {"data": state["active"]}
    elif operation == "session.list":
        if "parentID" in params:
            rows = [
                s for s in sessions.values() if s.get("parentID") == params["parentID"]
            ]
            result = {"data": rows, "cursor": {}}
        else:
            cursor = params.get("cursor")
            if cursor:
                assert set(params) == {"cursor", "limit"}, "cursor carries filters"
                filters = state["filters"]
                offset = int(cursor.removeprefix("page-"))
            else:
                filters = state["filters"] = params
                offset = 0
            rows = sorted(sessions.values(), key=lambda s: -s["time"]["updated"])
            archived = filters.get("archived")
            if archived is not None:
                rows = [
                    s
                    for s in rows
                    if bool(s["time"].get("archived")) == (archived == "true")
                ]
            if "search" in filters:
                rows = [
                    s for s in rows if filters["search"].lower() in s["title"].lower()
                ]
            # Force pagination even though the client asks for 100.
            result = {"data": rows[offset : offset + 2], "cursor": {}}
            # v2.2.5 can return a next cursor on the final nonempty page.
            if result["data"]:
                result["cursor"]["next"] = f"page-{offset + 2}"
    elif operation == "session.get":
        result = {"data": sessions[session_id]}
    elif operation == "model.list":
        result = {"data": state.get("models", [])}
    elif operation == "session.message.list":
        assert 1 <= int(params["limit"]) <= 200
        assert params["order"] == "desc"
        rows = [
            m
            for m in state["messages"]
            if m["type"] == params["type"]
            and m.get("sessionID", session_id) == session_id
        ]
        result = {
            "data": sorted(rows, key=lambda m: -m["time"]["created"])[
                : int(params["limit"])
            ],
            "cursor": {},
        }
    elif operation == "session.message.get":
        result = {
            "data": next(
                m
                for m in state["messages"]
                if m["id"] == params["messageID"]
                and m.get("sessionID", session_id) == session_id
            )
        }
    elif operation == "session.inbox.list":
        result = {"data": state["inbox"].get(session_id, [])}
    elif operation in {"session.permission.list", "session.form.list"}:
        result = {"data": state.get(operation, {}).get(session_id, [])}
    elif operation == "session.create":
        assert set(body) <= {"location", "agent", "title", "model"}
        assert body["agent"] != "opencode"
        session_id = f"ses_created{len(sessions)}"
        sessions[session_id] = {"id": session_id, "time": {}, **body}
        result = {"data": sessions[session_id]}
    elif operation == "session.prompt":
        assert set(body) == {"text", "delivery"}, "must preserve the target selection"
        assert body["delivery"] in {"queue", "steer"}
        result = {
            "data": {
                "id": "msg_fixtureInput",
                "time": {"created": 1000},
                "delivery": body["delivery"],
            }
        }
    elif operation == "session.update":
        assert set(body) == {"archived"}
        if body["archived"]:
            sessions[session_id]["time"]["archived"] = 1000
        else:
            sessions[session_id]["time"].pop("archived", None)
        result = None
    elif operation == "experimental.session.wait":
        result = None
    else:
        sys.exit(f"unexpected fixture operation: {operation}")

    state_path.write_text(json.dumps(state))
    if result is not None:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
