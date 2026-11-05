# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML>=6,<7"]
# ///
"""Portable bundle contracts and offline helper tests; no live API calls."""

from datetime import datetime, timedelta, timezone
from copy import deepcopy
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/session-coordination/scripts/sessions"


def frontmatter(path):
    match = re.match(r"\A---\n(.*?)\n---\n(.*)", path.read_text(), re.S)
    if not match:
        raise ValueError(f"Missing frontmatter: {path.name}")
    return yaml.safe_load(match[1]), match[2]


class BundleContracts(unittest.TestCase):
    def test_primary_and_ordered_permissions(self):
        agent, body = frontmatter(ROOT / "agents/opencode.md")
        self.assertEqual(agent["mode"], "primary")
        self.assertRegex(agent["model"], r"^[^/\s#]+/[^\s#]+(?:#[^\s#]+)?$")
        self.assertLessEqual(len(body.split()), 260)
        rules = agent["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        for rule in rules:
            self.assertEqual(set(rule), {"action", "resource", "effect"})
            self.assertIn(rule["effect"], {"allow", "ask", "deny"})

        def effect(action, resource):
            return next(
                (
                    r["effect"]
                    for r in reversed(rules)
                    if fnmatch.fnmatchcase(action, r["action"])
                    and fnmatch.fnmatchcase(resource, r["resource"])
                ),
                "ask",
            )

        for action in ("edit", "subagent", "execute"):
            self.assertEqual(effect(action, "*"), "deny")
        self.assertEqual(effect("skill", "session-coordination"), "allow")
        self.assertEqual(effect("skill", "customize-opencode"), "deny")
        for command in (
            "git push --force origin",
            "git push -f origin",
            "git reset --hard HEAD",
            "sudo ls",
            "git commit -m fixture",
            "git -C /project push origin",
            "gh pr create --draft",
            "gh pr merge 1",
            "opencode service restart",
            "opencode api session.remove --param sessionID=ses_example",
        ):
            self.assertEqual(effect("shell", command), "deny", command)
        self.assertEqual(
            effect("shell", "opencode api session.list --param limit=100"), "allow"
        )

    def test_skill_is_lazy_and_helper_is_executable(self):
        skill, _ = frontmatter(ROOT / "skills/session-coordination/SKILL.md")
        self.assertEqual(skill["name"], "session-coordination")
        self.assertTrue(skill["description"])
        self.assertIsNot(skill.get("autoinvoke"), True)
        self.assertTrue(os.access(HELPER, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", HELPER]).returncode, 0)

    def test_commands_inherit_model_and_do_not_execute_interpolated_shell(self):
        for name in ("session-review", "session-act"):
            config, body = frontmatter(ROOT / "commands" / f"{name}.md")
            self.assertEqual(set(config), {"description", "subagent"})
            self.assertIs(config["subagent"], False)
            self.assertNotIn("!`", body)  # Shell expansion bypasses agent permissions.
            self.assertIn("$ARGUMENTS", body)
        review = (ROOT / "commands/session-review.md").read_text()
        self.assertIn("read-only", review)
        self.assertIn("unknown", review)
        action = (ROOT / "commands/session-act.md").read_text()
        for contract in (
            "S send ID queue",
            "S start",
            "in their client",
            "explicit",
            "admitted",
        ):
            self.assertIn(contract, action)

    def test_review_prompt_requires_separate_states_short_output_and_explicit_actions(
        self,
    ):
        body = (ROOT / "commands/session-review.md").read_text()
        for contract in (
            "BOTH `sessions` and `triage`",
            "350 words",
            "work status",
            "input_percent",
            "exact IDs",
            "duplicate new session",
            "Suggestions never execute",
        ):
            self.assertIn(contract, body)


class ReviewEvalContracts(unittest.TestCase):
    def test_prompt_uses_synthetic_evidence_without_leaking_expected_answers(self):
        result = subprocess.run(
            [sys.executable, ROOT / "tests/review_eval.py", "prompt"],
            capture_output=True,
            text=True,
            check=True,
        )
        packet = result.stdout.split("<cases>\n", 1)[1].split("\n</cases>", 1)[0]
        cases = json.loads(packet)
        self.assertEqual(len(cases), 8)
        self.assertTrue(all(set(c) == {"id", "evidence"} for c in cases))
        self.assertIn("Do not call tools, APIs, or models", result.stdout)
        self.assertNotIn("$ARGUMENTS", result.stdout)

    def test_grader_accepts_reference_contract_and_rejects_unsafe_or_incomplete_answers(
        self,
    ):
        cases = json.loads((ROOT / "tests/review_cases.json").read_text())
        response = {
            "cases": [
                {
                    "id": c["id"],
                    **{
                        key: value[0] if isinstance(value, list) else value
                        for key, value in c["expected"].items()
                    },
                    "reason": "Synthetic reference answer for grader unit testing only.",
                }
                for c in cases
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "response.json"

            def check(answer, code):
                filename.write_text(json.dumps(answer))
                result = subprocess.run(
                    [sys.executable, ROOT / "tests/review_eval.py", "grade", filename],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return result.stdout

            self.assertIn("8/8 objective cases passed", check(response, 0))
            for key, value in (
                ("work_status", "reported-complete"),
                ("automatic_actions", True),
                ("target_session", "ses_replacement"),
            ):
                bad = deepcopy(response)
                bad["cases"][0][key] = value
                self.assertIn(key, check(bad, 1))
            missing = deepcopy(response)
            missing["cases"].pop()
            self.assertIn("missing, duplicate, or unknown", check(missing, 1))


class HelperContracts(unittest.TestCase):
    def setUp(self):
        for executable in ("bash", "jq", "timeout"):
            if not shutil.which(executable):
                self.fail(f"Required test executable missing: {executable}")
        self.temp = tempfile.TemporaryDirectory(prefix="coordinator-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state_path = self.directory / "state.json"
        fake = self.directory / "opencode"
        # Pin the fixture interpreter; never fall through to a real opencode.
        fake.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests/fake_opencode.py"}" "$@"\n'
        )
        fake.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{self.directory}{os.pathsep}{os.environ['PATH']}",
            "COORD_TEST_STATE": str(self.state_path),
            "OPENCODE_SESSION_ID": "ses_self",
            "TMPDIR": str(self.directory),
            "TZ": "UTC",
        }
        now = int(datetime.now(timezone.utc).timestamp() * 1000) - 60_000

        def session(name, title, directory="/projects/example", **extra):
            return {
                "id": name,
                "title": title,
                "agent": "direct",
                "projectID": "project_fixture",
                "location": {"directory": directory},
                "outcome": "succeeded",
                "model": {"providerID": "example", "id": "model", "variant": "medium"},
                "time": {
                    "created": now - 1000,
                    "busy": now - 500,
                    "idle": now,
                    "updated": now,
                },
                **extra,
            }

        self.state = {
            "sessions": {
                "ses_self": session("ses_self", "Coordinator", agent="opencode"),
                "ses_root": session(
                    "ses_root", "Example API work", "/projects/example-api"
                ),
                "ses_child": session("ses_child", "Review work", parentID="ses_root"),
                "ses_fork": session(
                    "ses_fork",
                    "Independent continuation",
                    fork={"sessionID": "ses_root"},
                ),
                "ses_peer": session("ses_peer", "Peer coordinator", agent="opencode"),
                "ses_other": session("ses_other", "Other project", "/projects/other"),
                "ses_archive": session("ses_archive", "Older completed work"),
            },
            "calls": [],
            "active": {"ses_other": {"type": "running"}},
            "inbox": {},
            "messages": [],
        }
        self.state["sessions"]["ses_archive"]["time"]["archived"] = now
        for index, (kind, content) in enumerate(
            (
                ("user", {"text": "Where did we leave off?"}),
                (
                    "assistant",
                    {
                        "agent": "direct",
                        "content": [
                            {"type": "reasoning", "text": "REASONING_MUST_NOT_LEAK"},
                            {
                                "type": "text",
                                "text": "Finished tests; next step is review. password=fixture-secret-value",
                            },
                            {
                                "type": "tool",
                                "name": "shell",
                                "state": {
                                    "status": "completed",
                                    "input": {"command": "test-example"},
                                    "content": "RAW_TOOL_OUTPUT_MUST_NOT_LEAK",
                                },
                            },
                        ],
                    },
                ),
                ("idle", {"outcome": "succeeded"}),
            )
        ):
            self.state["messages"].append(
                {
                    "type": kind,
                    "id": f"msg_fixture{index}",
                    "time": {"created": now + index},
                    **content,
                }
            )
        self.write_state()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def run_helper(self, *args, code=0, stdin=None):
        result = subprocess.run(
            [HELPER, *args],
            env=self.environment,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return result.stdout + result.stderr

    def test_global_discovery_pagination_children_and_archive_filter(self):
        output = self.run_helper("recent", "--kids")
        for expected in (
            "/projects/example-api",
            "/projects/other",
            "fork-of:ses_root",
            "└ ses_child",
            "SELF",
        ):
            self.assertIn(expected, output)
        self.assertNotIn("ses_archive", output)
        calls = [c for c in self.read_state()["calls"] if c[0] == "session.list"]
        self.assertGreater(len(calls), 1)
        self.assertNotIn("directory", calls[0][1])
        self.assertEqual(calls[0][1]["archived"], "false")
        self.assertEqual(calls[-1][1]["cursor"], "page-6")  # Empty terminal page.

    def test_matching_and_historical_archive_search(self):
        output = self.run_helper("recent", "--match", "Example API")
        self.assertIn("ses_root", output)
        self.assertNotIn("ses_other", output)
        archived = self.run_helper("recent", "--only-archived", "--hours", "0")
        self.assertIn("ses_archive", archived)
        self.assertNotIn("ses_root", archived)
        self.assertIn(
            "ses_archive", self.run_helper("recent", "--archived", "--search", "Older")
        )

    def test_yesterday_filter(self):
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        timestamp = (
            int(
                datetime.combine(
                    yesterday, datetime.min.time(), timezone.utc
                ).timestamp()
                * 1000
            )
            + 23 * 3_600_000  # Regression: +1 used to be parsed as a timezone.
        )
        self.state["sessions"]["ses_root"]["time"] = {
            "created": timestamp - 1000,
            "busy": timestamp - 500,
            "idle": timestamp,
            "updated": timestamp,
        }
        # A child active today would correctly move its parent's group to today.
        self.state["sessions"]["ses_child"]["time"] = dict(
            self.state["sessions"]["ses_root"]["time"]
        )
        self.write_state()
        output = self.run_helper("recent", "--day", "yesterday")
        self.assertIn("ses_root", output)
        self.assertNotIn("ses_other", output)

    def test_show_and_bounded_redacted_history(self):
        self.state["inbox"]["ses_root"] = [
            {"delivery": "queue", "type": "user", "payload": {"text": "Review next"}}
        ]
        self.write_state()
        output = self.run_helper("show", "ses_root")
        self.assertIn("[queue user] Review next", output)
        self.assertIn("children 1", output)
        output = self.run_helper("tail", "ses_root", "--turns", "1")
        self.assertIn("next step is review", output)
        self.assertIn("<redacted>", output)
        for text in (
            "REASONING_MUST_NOT_LEAK",
            "RAW_TOOL_OUTPUT_MUST_NOT_LEAK",
            "fixture-secret-value",
        ):
            self.assertNotIn(text, output)

    def test_self_targeting_is_refused(self):
        for args, code in (
            (["send", "ses_self", "queue", "Do not deliver"], 2),
            (["archive", "ses_self"], 1),
            (["wait", "ses_self", "1"], 2),
        ):
            self.assertIn("refusing to target", self.run_helper(*args, code=code))
        self.assertFalse(
            any(
                c[0]
                in {"session.prompt", "session.update", "experimental.session.wait"}
                for c in self.read_state()["calls"]
            )
        )

    def test_peer_and_archived_send_guards(self):
        self.assertIn(
            "another coordinator",
            self.run_helper("send", "ses_peer", "queue", "No", code=2),
        )
        self.assertIn(
            "archived; restore it first",
            self.run_helper("send", "ses_archive", "queue", "No", code=2),
        )

    def test_prompt_contract_preserves_selection(self):
        for mode in ("queue", "steer"):
            output = self.run_helper(
                "send", "ses_other", mode, "-", stdin="Finish scoped work; no push."
            )
            self.assertIn("Admitted is not completed", output)
        calls = [c for c in self.read_state()["calls"] if c[0] == "session.prompt"]
        self.assertEqual([c[2]["delivery"] for c in calls], ["queue", "steer"])
        for _, _, body in calls:
            self.assertEqual(set(body), {"text", "delivery"})

    def test_review_context_evidence_redaction_and_no_mutations(self):
        assistant = self.state["messages"][1]
        assistant.update(
            {
                "model": {"providerID": "example", "id": "last", "variant": "high"},
                "tokens": {
                    "input": 100,
                    "output": 300,
                    "cache": {"read": 200, "write": 50},
                },
                "finish": "stop",
            }
        )
        self.state["models"] = [
            {
                "providerID": "example",
                "id": "last",
                "limit": {"context": 1000, "input": 800},
                "headers": {"Authorization": "MODEL_SETTINGS_MUST_NOT_LEAK"},
            }
        ]
        self.state["session.form.list"] = {
            "ses_root": [{"id": "form_fixture", "title": "Choose scope"}]
        }
        self.write_state()
        output = self.run_helper("review")
        data = json.loads(output)
        row = next(r for r in data["sessions"] if r["id"] == "ses_root")
        self.assertEqual(data["sessions"][0]["id"], "ses_root")
        self.assertEqual(row["attention"], "needs-user")
        self.assertEqual(row["context"]["estimate"], 350)
        self.assertEqual(row["context"]["percent"], 35)
        self.assertEqual(row["context"]["capacity"], 1000)
        self.assertEqual(row["last_used_model"], "example/last#high")
        self.assertEqual(row["selected_model"], "example/model#medium")
        self.assertTrue(row["work_completion"].startswith("unknown"))
        self.assertIn("not checked", row["publication"])
        self.assertIn("UTC", data["as_of"])
        for secret in (
            "fixture-secret-value",
            "REASONING_MUST_NOT_LEAK",
            "RAW_TOOL_OUTPUT_MUST_NOT_LEAK",
            "MODEL_SETTINGS_MUST_NOT_LEAK",
        ):
            self.assertNotIn(secret, output)
        calls = self.read_state()["calls"]
        self.assertTrue(
            all(
                c[0] not in {"session.create", "session.prompt", "session.update"}
                for c in calls
            )
        )
        self.assertFalse(any(c[0] == "session.context" for c in calls))

    def test_review_keeps_old_active_child_and_reports_caps(self):
        old = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
        for s in self.state["sessions"].values():
            s["time"] = {"created": old - 10, "busy": old, "updated": old}
        self.state["active"] = {"ses_child": {"type": "running"}}
        self.write_state()
        data = json.loads(self.run_helper("review", "--max", "1"))
        self.assertEqual(data["sessions"][0]["id"], "ses_root")
        self.assertEqual(data["sessions"][0]["attention"], "running")
        self.assertEqual(data["sessions"][0]["children"][0]["id"], "ses_child")
        self.assertIsNone(data["sessions"][0]["context"]["estimate"])
        self.run_helper("review", "--max", "0", code=2)

    def test_review_surfaces_child_permission_without_claiming_whole_tree_checked(self):
        self.state["active"] = {"ses_child": {"type": "running"}}
        self.state["session.permission.list"] = {"ses_child": [{"id": "pending_child"}]}
        self.write_state()
        data = json.loads(self.run_helper("review"))
        root = next(r for r in data["sessions"] if r["id"] == "ses_root")
        self.assertEqual(root["attention"], "needs-user")
        self.assertEqual(root["child_blockers"][0]["permissions"], 1)
        self.assertIn("at most eight", root["child_coverage"])

    def test_review_limit_self_exclusion_and_archived_active_filter(self):
        data = json.loads(self.run_helper("review", "--max", "1"))
        self.assertEqual(data["inspected_roots"], 1)
        self.assertGreater(data["omitted_roots"], 0)
        self.assertNotEqual(data["sessions"][0]["id"], "ses_self")
        self.state["active"] = {"ses_archive": {"type": "running"}}
        self.write_state()
        data = json.loads(self.run_helper("review"))
        self.assertNotIn("ses_archive", [r["id"] for r in data["sessions"]])

    def test_review_failed_inspection_is_unknown_and_hides_errors(self):
        self.state["fail_operations"] = ["model.list", "session.form.list"]
        self.write_state()
        output = self.run_helper("review")
        # Failures go to stderr, JSON goes to stdout; run separately to parse stdout.
        self.assertIn("Inspection incomplete", output)
        self.assertIn('"capacity": null', output)
        self.assertNotIn("fixture-failure-secret", output)

    def test_review_zero_usage_is_unknown_not_zero_percent(self):
        self.state["messages"][1]["tokens"] = {
            "input": 0,
            "output": 0,
            "cache": {"read": 0, "write": 0},
        }
        self.write_state()
        data = json.loads(self.run_helper("review", "--match", "Example API"))
        self.assertIsNone(data["sessions"][0]["context"]["estimate"])
        self.assertIsNone(data["sessions"][0]["context"]["percent"])

    def test_review_scans_blockers_before_detail_limit(self):
        old = self.state["sessions"]["ses_root"]["time"]["updated"] - 86_400_000
        self.state["sessions"]["ses_root"]["time"] = {
            "created": old - 1000,
            "busy": old - 500,
            "idle": old,
            "updated": old,
        }
        for i in range(12):
            s = deepcopy(self.state["sessions"]["ses_other"])
            s.update(id=f"ses_recent{i}", title=f"Settled work {i}")
            self.state["sessions"][s["id"]] = s
        self.state["session.form.list"] = {
            "ses_root": [{"id": "form_old", "title": "Approval needed"}]
        }
        self.write_state()
        data = json.loads(self.run_helper("review", "--max", "1"))
        self.assertEqual(data["sessions"][0]["id"], "ses_root")
        self.assertEqual(
            data["sessions"][0]["work_status"]["state"], "needs-your-decision"
        )
        self.assertGreater(data["blocker_scanned_roots"], 10)
        self.assertEqual(data["unscanned_roots"], 0)
        self.assertEqual(len(data["triage"]), data["blocker_scanned_roots"] - 1)
        self.state = self.read_state()
        self.state["calls"] = []
        self.write_state()
        capped = json.loads(self.run_helper("review", "--max", "1", "--scan-max", "2"))
        self.assertEqual(capped["blocker_scanned_roots"], 2)
        self.assertGreater(capped["unscanned_roots"], 0)
        self.run_helper("review", "--max", "3", "--scan-max", "2", code=2)

    def test_review_keeps_free_text_decisions_in_triage_without_regex_classifier(self):
        for m in self.state["messages"]:
            m["sessionID"] = "ses_root"
        self.state["messages"][1].update(
            finish="stop",
            content=[
                {
                    "type": "text",
                    "text": "Tests passed. Shall I publish the change? No push yet.",
                }
            ],
        )
        self.state["sessions"]["ses_root"]["time"]["idle"] -= 10000
        self.state["sessions"]["ses_root"]["time"]["busy"] -= 10000
        self.write_state()
        data = json.loads(self.run_helper("review", "--max", "1"))
        row = next(r for r in data["triage"] if r["id"] == "ses_root")
        self.assertIn("Shall I publish", row["reply"])
        self.assertIn("selected_model", row)
        self.assertIn("input_percent", row["context"])
        self.assertEqual(row["work_status"]["state"], "unknown")
        self.assertNotEqual(data["sessions"][0]["id"], "ses_root")

    def test_review_reports_input_budget_and_progress_separately(self):
        assistant = self.state["messages"][1]
        assistant.update(
            model={"providerID": "example", "id": "used", "variant": "high"},
            tokens={"input": 40, "cache": {"read": 800, "write": 0}},
        )
        completed = assistant["time"]["created"] + 30_000
        assistant["time"]["completed"] = completed
        self.state["models"] = [
            {
                "providerID": "example",
                "id": "used",
                "limit": {"context": 2000, "input": 1000},
            }
        ]
        self.write_state()
        data = json.loads(self.run_helper("review", "--match", "Example API"))
        row = data["sessions"][0]
        self.assertEqual(row["context"]["percent"], 42)
        self.assertEqual(row["context"]["input_percent"], 84)
        self.assertEqual(row["context"]["input_headroom_estimate"], 160)
        self.assertTrue(row["context"]["input_warning"])
        self.assertEqual(row["context"]["model"], "example/used#high")
        self.assertEqual(row["selected_model"], "example/model#medium")
        self.assertNotEqual(row["last_progress_at"], row["run_started_at"])

    def test_review_final_refresh_sees_completion_and_new_reply(self):
        self.state["active"] = {"ses_root": {"type": "running"}}
        sessions = deepcopy(self.state["sessions"])
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        sessions["ses_root"]["time"]["idle"] = now
        messages = deepcopy(self.state["messages"])
        for m in messages:
            m["sessionID"] = "ses_root"
        messages.append(
            {
                "id": "msg_done",
                "sessionID": "ses_root",
                "type": "assistant",
                "agent": "direct",
                "time": {"created": now},
                "finish": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": "Finished local checks; nothing published.",
                    }
                ],
            }
        )
        self.state["operation_events"] = {
            "session.active": [
                {},
                {"active": {}, "sessions": sessions, "messages": messages},
            ]
        }
        self.write_state()
        data = json.loads(self.run_helper("review", "--match", "Example API"))
        row = data["sessions"][0]
        self.assertEqual(row["execution"], "succeeded")
        self.assertNotEqual(row["attention"], "running")
        self.assertIn("Finished local checks", row["reply"])
        self.assertEqual(row["refresh"], "refreshed")
        self.assertEqual(data["refreshed_roots"], 1)
        self.assertEqual(
            sum(c[0] == "session.active" for c in self.read_state()["calls"]), 2
        )
        self.assertEqual(row["work_status"]["state"], "unknown")

    def test_review_uses_last_positive_measurement_not_streaming_zero_usage(self):
        for m in self.state["messages"]:
            m["sessionID"] = "ses_root"
        previous = self.state["messages"][1]
        previous.update(
            model={"providerID": "example", "id": "previous"},
            tokens={"input": 420, "cache": {"read": 0, "write": 0}},
        )
        self.state["messages"].append(
            {
                "id": "msg_streaming",
                "sessionID": "ses_root",
                "type": "assistant",
                "time": {"created": previous["time"]["created"] + 1000},
                "model": {"providerID": "example", "id": "new"},
                "content": [],
                "tokens": {"input": 0, "cache": {"read": 0, "write": 0}},
            }
        )
        self.state["models"] = [
            {
                "providerID": "example",
                "id": "previous",
                "limit": {"context": 628, "input": 500},
            }
        ]
        self.write_state()
        row = json.loads(self.run_helper("review", "--match", "Example API"))[
            "sessions"
        ][0]
        self.assertEqual(row["last_used_model"], "example/new")
        self.assertEqual(row["context"]["model"], "example/previous")
        self.assertEqual(row["context"]["input_percent"], 84)
        self.assertEqual(row["context"]["message_id"], previous["id"])

    def test_review_queued_work_is_not_recommended_for_duplicate_admission(self):
        self.state["inbox"]["ses_root"] = [
            {
                "delivery": "queue",
                "type": "user",
                "payload": {"text": "Finish the accepted fix."},
            }
        ]
        self.write_state()
        row = json.loads(self.run_helper("review", "--match", "Example API"))[
            "sessions"
        ][0]
        self.assertEqual(row["attention"], "queued")
        self.assertEqual(row["work_status"]["state"], "unfinished")
        self.assertIn("already queued", row["next_action"])
        self.assertFalse(
            any(c[0] == "session.prompt" for c in self.read_state()["calls"])
        )

    def test_review_refresh_failure_does_not_preserve_confident_running_state(self):
        self.state["active"] = {"ses_root": {"type": "running"}}
        self.state["operation_events"] = {
            "session.active": [{}, {"active": {}, "fail_operations": ["session.get"]}]
        }
        self.write_state()
        output = self.run_helper("review", "--match", "Example API")
        self.assertIn('"execution": "unknown"', output)
        self.assertIn('"refresh": "unavailable"', output)
        self.assertNotIn("fixture-failure-secret", output)

    def test_send_blocks_forms_permissions_and_children(self):
        for operation in ("session.form.list", "session.permission.list"):
            self.state[operation] = {"ses_root": [{"id": "pending_fixture"}]}
            self.write_state()
            self.assertIn(
                "answer it in the client",
                self.run_helper("send", "ses_root", "queue", "Yes", code=2),
            )
            self.state[operation] = {}
        self.write_state()
        self.assertIn(
            "is a child", self.run_helper("send", "ses_child", "queue", "Go", code=2)
        )
        self.assertFalse(
            any(c[0] == "session.prompt" for c in self.read_state()["calls"])
        )

    def test_start_root_with_explicit_model_and_safe_admission(self):
        output = self.run_helper(
            "start",
            "--directory",
            str(self.directory),
            "--agent",
            "direct",
            "--title",
            "Bounded work",
            "--model",
            "example/family/selected#high",
            "-",
            stdin="Inspect only; no edits, commits, or push.",
        )
        self.assertIn("created ses_created", output)
        self.assertIn("Admitted is not completed", output)
        calls = self.read_state()["calls"]
        body = next(c[2] for c in calls if c[0] == "session.create")
        self.assertEqual(
            body["model"],
            {"providerID": "example", "id": "family/selected", "variant": "high"},
        )
        self.assertNotIn("permissions", body)
        self.assertNotIn("parentID", body)
        self.assertEqual(
            next(c[2] for c in calls if c[0] == "session.prompt")["delivery"], "queue"
        )

    def test_start_rejects_coordinators_and_same_checkout_owners(self):
        args = [
            "start",
            "--directory",
            str(self.directory),
            "--agent",
            "opencode",
            "--title",
            "No",
            "-",
        ]
        self.assertIn(
            "never create another coordinator",
            self.run_helper(*args, code=2, stdin="No"),
        )
        self.state["sessions"]["ses_other"]["location"]["directory"] = str(
            self.directory
        )
        self.write_state()
        args[args.index("opencode")] = "direct"
        self.assertIn(
            "active in this checkout", self.run_helper(*args, code=2, stdin="No")
        )
        self.assertFalse(
            any(c[0] == "session.create" for c in self.read_state()["calls"])
        )

    def test_start_partial_admission_failure_keeps_created_id(self):
        self.state["fail_operations"] = ["session.prompt"]
        self.write_state()
        output = self.run_helper(
            "start",
            "--directory",
            str(self.directory),
            "--agent",
            "direct",
            "--title",
            "Bounded work",
            "-",
            stdin="Inspect only",
            code=1,
        )
        self.assertIn("created ses_created", output)
        self.assertIn("inspect this ID before retrying", output)
        self.assertNotIn("fixture-failure-secret", output)

    def test_start_ambiguous_create_error_does_not_retry(self):
        self.state["fail_operations"] = ["session.create"]
        self.write_state()
        output = self.run_helper(
            "start",
            "--directory",
            str(self.directory),
            "--agent",
            "direct",
            "--title",
            "Bounded work",
            "-",
            stdin="Inspect only",
            code=1,
        )
        self.assertIn("creation uncertain", output)
        calls = self.read_state()["calls"]
        self.assertEqual(sum(c[0] == "session.create" for c in calls), 1)
        self.assertFalse(any(c[0] == "session.prompt" for c in calls))

    def test_start_detects_subdirectory_and_symlink_as_same_git_checkout(self):
        repo = self.directory / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subdir = repo / "subdir"
        subdir.mkdir()
        alias = self.directory / "alias"
        alias.symlink_to(subdir)
        self.state["sessions"]["ses_other"]["location"]["directory"] = str(repo)
        self.write_state()
        output = self.run_helper(
            "start",
            "--directory",
            str(alias),
            "--agent",
            "direct",
            "--title",
            "No concurrent writer",
            "-",
            stdin="Implement",
            code=2,
        )
        self.assertIn("active in this checkout", output)
        self.assertFalse(
            any(c[0] == "session.create" for c in self.read_state()["calls"])
        )

    def test_delivery_missing_checkout_is_unknown(self):
        output = self.run_helper("delivery", "ses_root")
        self.assertIn("publication and cleanup unknown", output)

    def test_delivery_separates_session_directory_from_evidenced_checkout(self):
        repo = self.directory / "work-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        self.state["messages"] = [
            {
                "id": "msg_checkout",
                "sessionID": "ses_root",
                "type": "assistant",
                "content": [
                    {
                        "type": "tool",
                        "name": "shell",
                        "state": {
                            "status": "completed",
                            "input": {
                                "workdir": str(repo),
                                "command": "git status -sb",
                            },
                            "content": "RAW_TOOL_OUTPUT_MUST_NOT_LEAK",
                        },
                    }
                ],
            }
        ]
        self.write_state()
        data = json.loads(
            self.run_helper(
                "delivery",
                "ses_root",
                "--directory",
                str(repo),
                "--evidence-message",
                "msg_checkout",
            )
        )
        self.assertEqual(data["session_directory"], "/projects/example-api")
        self.assertEqual(data["work_directory"], str(repo))
        self.assertEqual(data["checkout_relation"], "recorded-tool-workdir")
        self.assertNotIn("RAW_TOOL_OUTPUT", json.dumps(data))
        unverified = json.loads(
            self.run_helper("delivery", "ses_root", "--directory", str(repo))
        )
        self.assertEqual(unverified["checkout_relation"], "unverified-override")
        self.state = self.read_state()
        self.state["messages"][0]["content"] = [
            {"type": "text", "text": f"Work was done in `{repo}`."}
        ]
        self.write_state()
        reported = json.loads(
            self.run_helper(
                "delivery",
                "ses_root",
                "--directory",
                str(repo),
                "--evidence-message",
                "msg_checkout",
            )
        )
        self.assertEqual(reported["checkout_relation"], "session-reported-path")
        self.state = self.read_state()
        self.state["messages"][0]["content"] = [
            {"type": "text", "text": f"Work was done in `{repo}-different`."}
        ]
        self.write_state()
        prefix = json.loads(
            self.run_helper(
                "delivery",
                "ses_root",
                "--directory",
                str(repo),
                "--evidence-message",
                "msg_checkout",
            )
        )
        self.assertEqual(prefix["checkout_relation"], "unverified-override")
        self.state = self.read_state()
        self.state["messages"][0]["sessionID"] = "ses_other"
        self.write_state()
        foreign = self.run_helper(
            "delivery",
            "ses_root",
            "--directory",
            str(repo),
            "--evidence-message",
            "msg_checkout",
        )
        self.assertIn('"checkout_relation": "unverified-override"', foreign)

    def test_delivery_local_remote_and_cleanup_evidence(self):
        repo = self.directory / "repo"
        remote = self.directory / "remote.git"

        def git(*args, cwd=repo):
            return subprocess.check_output(
                ["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True
            ).strip()

        repo.mkdir()
        git("init", "-b", "feature")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        )
        sha = git("rev-parse", "HEAD")
        git("init", "--bare", str(remote))
        git("remote", "add", "origin", str(remote))
        git("push", "origin", "feature")
        self.state["sessions"]["ses_root"]["location"]["directory"] = str(repo)
        self.write_state()
        data = json.loads(
            self.run_helper(
                "delivery", "ses_root", "--branch", "feature", "--commit", sha
            )
        )
        self.assertEqual(data["pushed"], "verified-on-remote-branch")
        self.assertEqual(data["local_commit"], "present")
        self.assertEqual(data["local_branch"], "present")
        self.assertEqual(data["remote_branch"], "present")
        self.assertEqual(data["worktrees_for_branch"], "1")
        self.assertEqual(data["dirty_entries"], 0)
        git("branch", "-m", "keep")
        git("push", "origin", "--delete", "feature")
        data = json.loads(
            self.run_helper(
                "delivery", "ses_root", "--branch", "feature", "--commit", sha
            )
        )
        self.assertEqual(data["local_branch"], "absent")
        self.assertEqual(data["remote_branch"], "absent")
        self.assertEqual(data["pushed"], "unknown")
        self.assertEqual(data["worktrees_for_branch"], "0")
        self.assertIn("absence does not prove", data["cleanup"])

    def test_delivery_sanitizes_urls_and_never_qualifies_unavailable_remote(self):
        repo = self.directory / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://user:fixture-url-secret@forge.example.invalid/team/repo.git?token=fixture-query-secret",
            ],
            check=True,
        )
        self.state["sessions"]["ses_root"]["location"]["directory"] = str(repo)
        self.write_state()
        # No branch on an unborn checkout? symbolic HEAD names a branch, but avoid network.
        fake_git = self.directory / "git"
        real_git = shutil.which("git")
        fake_git.write_text(
            f'#!/bin/sh\ncase "$*" in *ls-remote*) exit 1;; esac\nexec "{real_git}" "$@"\n'
        )
        fake_git.chmod(0o755)
        data = json.loads(self.run_helper("delivery", "ses_root"))
        self.assertEqual(
            data["repository_url"], "https://forge.example.invalid/team/repo"
        )
        self.assertEqual(data["remote_branch"], "unknown")
        self.assertEqual(data["pushed"], "unknown")
        self.assertNotIn("secret", json.dumps(data))

    def test_delivery_matches_pr_source_and_handles_missing_remote_branch(self):
        repo = self.directory / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "feature", str(repo)], capture_output=True, check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/example/repo.git",
            ],
            check=True,
        )
        self.state["sessions"]["ses_root"]["location"]["directory"] = str(repo)
        self.write_state()
        sha = "a" * 40
        fixture = self.directory / "prs.json"
        fixture.write_text(
            json.dumps(
                [
                    {
                        "number": 1,
                        "headRepository": {"nameWithOwner": "example/repo"},
                        "headRefOid": sha,
                        "state": "MERGED",
                        "url": "https://github.com/example/repo/pull/1",
                    },
                    {
                        "number": 2,
                        "headRepository": {"nameWithOwner": "someone/fork"},
                        "headRefOid": sha,
                        "state": "OPEN",
                    },
                ]
            )
        )
        fake_git = self.directory / "git"
        real_git = shutil.which("git")
        fake_git.write_text(
            f'#!/bin/sh\ncase "$*" in *ls-remote*) exit 0;; esac\nexec "{real_git}" "$@"\n'
        )
        fake_git.chmod(0o755)
        fake_gh = self.directory / "gh"
        fake_gh.write_text(f'#!/bin/sh\ncat "{fixture}"\n')
        fake_gh.chmod(0o755)
        data = json.loads(
            self.run_helper(
                "delivery", "ses_root", "--branch", "feature", "--commit", sha
            )
        )
        self.assertEqual(data["remote_branch"], "absent")
        self.assertEqual(data["pushed"], "verified-in-pr-head")
        self.assertEqual(data["prs_check"], "verified-now")
        self.assertEqual([p["number"] for p in data["prs"]], [1])
        self.assertEqual(data["prs"][0]["state"], "MERGED")
        self.assertEqual(
            data["commit_url"], f"https://github.com/example/repo/commit/{sha}"
        )

    def test_archive_restore_verification_and_running_guard(self):
        output = self.run_helper("archive", "ses_other", code=1)
        self.assertIn("is running; skipped", output)
        self.assertIn("(verified)", self.run_helper("archive", "ses_root"))
        self.assertIn("archived", self.read_state()["sessions"]["ses_root"]["time"])
        self.assertIn("already archived", self.run_helper("archive", "ses_root"))
        self.assertIn("restored (verified)", self.run_helper("restore", "ses_root"))
        self.assertNotIn("archived", self.read_state()["sessions"]["ses_root"]["time"])
        self.assertIn(
            "(verified)", self.run_helper("archive", "--running", "ses_other")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
