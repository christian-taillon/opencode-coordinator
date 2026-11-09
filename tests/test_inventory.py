"""Fast-path fixture proof only. Never calls installed vendor or lifecycle commands."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/session-coordination/scripts"
HELPER = SCRIPTS / "sessions"
PREFERRED_SCRATCH = Path("/tmp/opencode")


def records(packet):
    return [r for g in packet["groups"] for r in g["sessions"]]


def coverage(packet, name, machine="local"):
    return next(
        s for s in packet["coverage"] if s["source"] == name and s["machine"] == machine
    )


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="inventory-",
            dir=PREFERRED_SCRATCH if PREFERRED_SCRATCH.is_dir() else None,
        )
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.fixture = self.path / "fixture.json"
        self.calls_file = self.path / "calls.jsonl"
        for name in ("bash", "dirname", "python3"):
            (self.path / name).symlink_to(shutil.which(name))
        for vendor in ("opencode", "claude", "codex", "herdr", "git", "gh"):
            fake = self.path / vendor
            fake.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests/fake_inventory.py"}" {vendor} "$@"\n'
            )
            fake.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(self.path),
            "HOME": str(self.path),
            "INVENTORY_FIXTURE": str(self.fixture),
            "INVENTORY_CALLS": str(self.calls_file),
            "OPENCODE_SESSION_ID": "ses_self",
            "HERDR_ENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        now = int(time.time() * 1000) - 1000
        self.data = {"sessions": [], "active": {}}
        for i in range(90):
            self.data["sessions"].append(
                {
                    "id": f"ses_root{i:03}",
                    "projectID": f"project_{i % 3}",
                    "location": {"directory": f"/projects/{i % 3}/same-name"},
                    "title": f"Synthetic work {i}",
                    "outcome": "failed" if i % 17 == 0 else "succeeded",
                    "time": {
                        "created": now - 10_000,
                        "updated": now - i,
                        "busy": now - 5000,
                        "idle": now - i,
                        "viewed": now + 1,
                    },
                }
            )
        for i in range(65):
            row = deepcopy(self.data["sessions"][i])
            row.update(id=f"ses_child{i:03}", parentID=f"ses_root{i:03}")
            self.data["sessions"].append(row)
        self.data["active"] = {"ses_child064": {"type": "running"}}

    def calls(self):
        return (
            [json.loads(line) for line in self.calls_file.read_text().splitlines()]
            if self.calls_file.exists()
            else []
        )

    def invoke(self, *args, operation="overview", code=0, decode=True):
        self.fixture.write_text(json.dumps(self.data))
        before = self.fixture.read_bytes()
        files = set(self.path.iterdir())
        start = time.monotonic()
        result = subprocess.run(
            [HELPER, operation, *args],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=12,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(self.fixture.read_bytes(), before)
        self.assertLessEqual(set(self.path.iterdir()) - files, {self.calls_file})
        self.assertNotIn("SECRET_DIAGNOSTIC", result.stdout + result.stderr)
        return (json.loads(result.stdout) if decode else result.stdout), elapsed

    def test_large_inventory_children_grouping_limits_and_no_expensive_calls(self):
        packet, elapsed = self.invoke("--json", "--max", "100")
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(
            packet["counts"],
            dict(records=155, roots=90, children=65, shown=90, remaining=0),
        )
        self.assertEqual(len(packet["groups"]), 3)
        root = next(r for r in records(packet) if r["id"] == "ses_root064")
        self.assertNotIn("running", root["attention"])
        self.assertIn("running", root["child_attention"])
        self.assertEqual(root["children"][0]["execution_state"], "running")
        self.assertEqual(root["work_status"], "not_assessed")
        self.assertEqual(root["delivery_status"], "not_assessed")
        self.assertEqual(root["pending_input"], "not_assessed")
        native_calls = [
            args[1] for vendor, args, _ in self.calls() if vendor == "opencode"
        ]
        self.assertEqual(native_calls.count("session.list"), 3)
        self.assertLessEqual(len(native_calls), 5)
        self.assertTrue(
            set(native_calls) <= {"session.list", "session.active", "server.info"}
        )
        self.assertFalse(any(v in ("git", "gh", "codex") for v, _, _ in self.calls()))
        self.assertEqual(
            [args for vendor, args, _ in self.calls() if vendor == "claude"],
            [["agents", "--json"]],
        )
        self.assertLess(elapsed, 2)
        output, _ = self.invoke(decode=False)
        self.assertLess(len(output), 6000)
        self.assertNotIn("ses_root", output)
        self.assertNotIn("ses_child", output)

    def test_empty_and_missing_sources_are_not_empty_success(self):
        self.data["sessions"] = []
        self.data["active"] = {}
        for name in ("opencode", "claude", "codex", "herdr"):
            (self.path / name).unlink()
        packet, _ = self.invoke("--json")
        self.assertEqual(packet["counts"]["records"], 0)
        self.assertTrue(packet["partial_failure"])
        self.assertTrue(all(s["status"] == "unavailable" for s in packet["coverage"]))

    def test_empty_observed_native_source(self):
        self.data.update(sessions=[], active={})
        packet, _ = self.invoke("--json")
        self.assertEqual(coverage(packet, "opencode")["status"], "observed")
        self.assertEqual(packet["counts"]["roots"], 0)

    def test_fixture_scratch_falls_back_to_tmpdir_when_preferred_directory_is_absent(
        self,
    ):
        fallback = self.path / "standard-temp"
        fallback.mkdir()
        code = (
            f"import sys;sys.path.insert(0,{str(ROOT / 'tests')!r});import test_inventory;"
            "test_inventory.PREFERRED_SCRATCH=test_inventory.Path(sys.argv[1]);"
            "case=test_inventory.InventoryTests();case.setUp();"
            "print(case.path.parent);case.doCleanups()"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.path / "absent-preferred")],
            env={**self.environment, "TMPDIR": str(fallback)},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(Path(result.stdout.strip()), fallback)

    def test_installed_executable_runs_outside_repository_without_alias(self):
        for name in ("mkdir", "install"):
            (self.path / name).symlink_to(shutil.which(name))
        env = {**self.environment, "OPENCODE_CONFIG_DIR": ""}
        subprocess.run(
            [ROOT / "install.sh"],
            env=env,
            cwd=self.path,
            check=True,
            capture_output=True,
        )
        installed = (
            self.path / ".config/opencode/skills/session-coordination/scripts/sessions"
        )
        self.fixture.write_text(json.dumps(self.data))
        result = subprocess.run(
            [installed, "overview", "--json"],
            env=env,
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        )
        packet = json.loads(result.stdout)
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(packet["counts"]["roots"], 90)
        self.assertFalse(list(installed.parent.glob("__pycache__")))

    def test_documented_existing_update_stages_commit_and_preserves_custom_agent(self):
        readme = (
            (ROOT / "README.md")
            .read_text()
            .split("<!-- existing-install-update -->", 1)[1]
        )
        script = re.search(r"```bash\n(.*?)\n```", readme, re.S)[1]
        paths = [
            line.strip()
            for line in re.search(r"files=\(\n(.*?)\n\)", script, re.S)[1].splitlines()
        ]
        repo = self.path / "published-bundle"
        repo.mkdir()
        env = {
            **os.environ,
            "HOME": str(self.path),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", repo, *args],
                env=env,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()

        git("init", "-b", "main")
        expected = {}
        for path in paths:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
            expected[path] = target.read_bytes()
        git("add", "--", *paths)
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Synthetic reviewed bundle",
        )
        revision = git("rev-parse", "HEAD")
        git("remote", "add", "origin", str(repo))
        (repo / paths[0]).write_text("UNREVIEWED_WORKING_TREE_CHANGE")
        dest = self.path / ".config/opencode"
        for path in paths:
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("PREVIOUS_BUNDLE_CONTENT")
        agent = dest / "agents/opencode.md"
        agent.parent.mkdir()
        customization = "---\nmodel: fixture/custom#high\npermissions: preserve-ordered-fixture-rules\n---\nCustom agent body.\n"
        agent.write_text(customization)
        config = dest / "opencode.json"
        config.write_text('{"fixture":"preserve"}')
        fallback = self.path / "standard-temp"
        fallback.mkdir()
        env["TMPDIR"] = str(fallback)
        script = script.replace("/tmp/opencode", str(self.path / "absent-preferred"))
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=repo,
            env=env,
            input=revision + "\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backups = list(self.path.glob(".config/opencode-coordinator-backup.*"))
        self.assertEqual(len(backups), 1)
        for path in paths:
            self.assertEqual((dest / path).read_bytes(), expected[path])
            self.assertEqual((backups[0] / path).read_text(), "PREVIOUS_BUNDLE_CONTENT")
        self.assertEqual(agent.read_text(), customization)
        self.assertEqual(config.read_text(), '{"fixture":"preserve"}')
        self.assertFalse((backups[0] / "agents").exists())
        self.assertTrue(
            os.access(dest / "skills/session-coordination/scripts/sessions", os.X_OK)
        )
        self.assertEqual(len(list(fallback.glob("coordinator-install.*"))), 1)

    def test_documented_update_scratch_prefers_existing_directory_otherwise_tmpdir(
        self,
    ):
        readme = (
            (ROOT / "README.md")
            .read_text()
            .split("<!-- existing-install-update -->", 1)[1]
        )
        script = re.search(r"```bash\n(.*?)\n```", readme, re.S)[1]
        scratch = "scratch=" + script.split("scratch=", 1)[1].split("\nbackup=", 1)[0]
        preferred, fallback = self.path / "preferred", self.path / "standard-temp"
        preferred.mkdir()
        fallback.mkdir()
        for candidate, expected in (
            (preferred, preferred),
            (self.path / "absent-preferred", fallback),
        ):
            with self.subTest(preferred=candidate):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        scratch.replace("/tmp/opencode", str(candidate))
                        + '\nprintf "%s\\n" "$stage"',
                    ],
                    env={**os.environ, "TMPDIR": str(fallback)},
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(Path(result.stdout.strip()).parent, expected)

    def test_project_filter_max_and_invalid_flags(self):
        packet, _ = self.invoke("--json", "--project", "project_1", "--max", "2")
        self.assertEqual(packet["counts"]["roots"], 30)
        self.assertEqual(packet["counts"]["remaining"], 28)
        self.assertTrue(packet["truncated"])
        for args in (
            ("--max", "0"),
            ("--days", "-1"),
            ("--machine", "--bad"),
            ("--machine", "local", "--all-machines"),
        ):
            self.invoke(*args, code=2, decode=False)

    def test_page_cap_and_remaining_coverage(self):
        self.data["page_size"] = 2
        packet, _ = self.invoke("--json")
        self.assertTrue(coverage(packet, "opencode")["truncated"])
        self.assertEqual(coverage(packet, "opencode")["scanned_records"], 12)
        self.assertTrue(packet["truncated"])
        self.assertEqual(
            sum(
                v == "opencode" and args[1] == "session.list"
                for v, args, _ in self.calls()
            ),
            5,
        )

    def test_oversized_server_page_is_bounded_and_reported(self):
        self.data["page_size"] = 1000
        packet, _ = self.invoke("--json", "--max", "100")
        source = coverage(packet, "opencode")
        self.assertTrue(source["truncated"])
        self.assertLessEqual(source["scanned_records"], 124)
        self.assertIn("session page exceeded requested limit", source["errors"])

    def test_old_active_and_missing_parent_are_fetched_but_self_is_excluded(self):
        old = int(time.time() * 1000) - 30 * 86_400_000
        for row in self.data["sessions"]:
            row["time"].update(updated=old, busy=old)
        self.data["sessions"][0]["id"] = "ses_self"
        packet, _ = self.invoke("--json")
        self.assertEqual(packet["counts"]["roots"], 1)
        self.assertEqual(records(packet)[0]["id"], "ses_root064")
        self.assertEqual(records(packet)[0]["children"][0]["id"], "ses_child064")
        self.assertNotIn("ses_self", json.dumps(packet))

    def test_archived_failed_unseen_and_success_is_not_completion(self):
        self.data["sessions"][0]["time"]["archived"] = 1
        self.data["sessions"][1]["time"]["viewed"] = 0
        packet, _ = self.invoke("--json", "--max", "100")
        roots = records(packet)
        self.assertNotIn("ses_root000", [r["id"] for r in roots])
        row = next(r for r in roots if r["id"] == "ses_root001")
        self.assertIn("unseen_outcome", row["attention"])
        self.assertEqual(row["execution_state"], "succeeded")
        self.assertEqual(row["work_status"], "not_assessed")

    def test_native_active_failure_is_unknown_not_idle(self):
        self.data["fail"] = ["opencode api session.active"]
        packet, _ = self.invoke("--json")
        self.assertTrue(packet["partial_failure"])
        self.assertEqual({r["execution_state"] for r in records(packet)}, {"unknown"})

    def test_invalid_titles_paths_secrets_and_unknown_fields(self):
        row = self.data["sessions"][0]
        row.update(
            title='\x1b[31mTOKEN="secret-value" password=other-secret ghp_' + "A" * 25,
            location={"directory": "/invalid\n/path"},
            subpath="../../escape",
            metadata={"reply": "TRANSCRIPT_MUST_NOT_LEAK"},
        )
        self.data["sessions"][1]["location"] = ["malformed"]
        packet, _ = self.invoke("--json")
        output = json.dumps(packet)
        for value in (
            "secret-value",
            "other-secret",
            "A" * 25,
            "TRANSCRIPT_MUST_NOT_LEAK",
            "u001b",
        ):
            self.assertNotIn(value, output)
        row = next(r for r in records(packet) if r["id"] == "ses_root000")
        self.assertIsNone(row["directory"])
        self.assertIsNone(row["worktree"])

    def test_human_display_budget_includes_unicode_and_coverage(self):
        for row in self.data["sessions"]:
            row["title"] = "界🤖" * 200
        output, _ = self.invoke("--max", "100", decode=False)
        estimate = sum(1 if ord(c) < 128 else 12 for c in output) / 3
        self.assertLess(estimate, 1500)
        self.assertIn("more records not displayed", output)
        for name in ("opencode", "claude", "codex", "herdr"):
            self.assertIn(f"{name}@local:", output)

    def test_representative_inventory_with_blocked_presence_claude_and_unreachable_remote(
        self,
    ):
        self.data["snapshots"] = {"local": self.snapshot(sid="ses_unmatched")}
        self.data["machines"] = [{"id": "remote", "enabled": True}]
        self.data["unreachable"] = ["remote"]
        self.data["claude"] = [
            {
                "id": "claude_fixture",
                "kind": "background",
                "state": "working",
                "name": "Claude fixture",
            }
        ]
        output, elapsed = self.invoke("--all-machines", decode=False)
        self.assertLess(elapsed, 2.5)
        self.assertLess(sum(1 if ord(c) < 128 else 12 for c in output) / 3, 1500)
        self.assertIn("Herdr blocked UI 1", output)
        self.assertIn("Claude reported blocked 0, working 1", output)
        self.assertIn("herdr@remote: unavailable", output)
        self.assertIn("reported blocked", output)
        self.assertIn("inspect Claude client", output)
        self.assertFalse(
            any(
                "--help" in args or "--version" in args or "--all" in args
                for _, args, _ in self.calls()
            )
        )

    def test_secret_shaped_native_parent_pane_reference_and_vendor_ids_are_rejected(
        self,
    ):
        aws = "AKIA1234567890123456"
        sk = "sk-abcdefghijklmnop"
        self.data["sessions"][0]["id"] = f"ses_{aws}"
        self.data["sessions"][1]["parentID"] = f"ses_{aws}"
        self.data["active"][f"ses_{aws}"] = {"type": "running"}
        snapshot = self.snapshot(sid=f"ses_{aws}")
        snapshot["result"]["snapshot"]["agents"].append({"pane_id": f"{sk}:p1"})
        self.data["snapshots"] = {"local": snapshot}
        self.data["claude"] = [{"id": sk, "kind": "background", "state": "working"}]
        self.data["machines"] = [{"id": sk, "enabled": True}]
        packet, _ = self.invoke("--json", "--all-machines", "--max", "100")
        for secret in (aws, sk):
            self.assertNotIn(secret, json.dumps(packet))
            self.assertFalse(
                any(secret in " ".join(args) for _, args, _ in self.calls())
            )
        self.assertEqual(coverage(packet, "opencode")["status"], "partial")
        self.assertEqual(coverage(packet, "herdr")["status"], "partial")
        self.assertTrue(coverage(packet, "claude")["errors"])
        self.assertTrue(
            all(not r["id"].startswith("<redacted>") for r in records(packet))
        )

    def test_secret_shaped_caller_host_emits_no_identity_and_queries_no_sources(self):
        code = (
            f"import sys,json,argparse;sys.path.insert(0,{str(SCRIPTS)!r});import inventory;"
            "inventory.CALLER_HOST='AKIA1234567890123456';"
            "args=argparse.Namespace(days=14,max=20,project='',machine=None,all_machines=False,offset=0);"
            "print(json.dumps(inventory.collect(args)))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
        )
        packet = json.loads(result.stdout)
        self.assertNotIn("AKIA1234567890123456", result.stdout)
        self.assertTrue(packet["partial_failure"])
        self.assertEqual(packet["counts"]["records"], 0)
        self.assertEqual(self.calls(), [])

    def test_unexpected_json_is_partial_and_no_raw_diagnostics(self):
        self.data["raw"] = {
            "opencode api session.active": "not-json",
            "claude agents --json": '{"secret":"SECRET_DIAGNOSTIC"}',
        }
        packet, _ = self.invoke("--json")
        self.assertTrue(packet["partial_failure"])
        self.assertEqual(coverage(packet, "claude")["status"], "unavailable")

    def test_deep_optional_json_is_unavailable_without_discarding_native_rows(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        self.data["raw"] = {
            "claude agents --json": nested,
            "herdr api snapshot": nested,
        }
        packet, _ = self.invoke("--json")
        self.assertTrue(packet["partial_failure"])
        self.assertEqual(coverage(packet, "opencode")["status"], "observed")
        self.assertEqual(coverage(packet, "claude")["status"], "unavailable")
        self.assertEqual(coverage(packet, "herdr")["status"], "unavailable")
        self.assertEqual(packet["counts"]["roots"], 90)

    def test_output_cap_and_invalid_parent_metadata_do_not_break_json(self):
        self.data["raw"] = {"claude agents --json": "X" * 2_000_001}
        self.data["sessions"][0]["parentID"] = ["invalid"]
        packet, _ = self.invoke("--json")
        self.assertEqual(coverage(packet, "claude")["status"], "unavailable")
        self.assertTrue(packet["partial_failure"])
        self.assertIn("output exceeded 2 MB", coverage(packet, "claude")["errors"])

    def test_nested_and_cyclic_parents_are_bounded_and_json_safe(self):
        self.data["sessions"][90]["parentID"] = "ses_child001"
        self.data["sessions"][2]["parentID"] = "ses_root003"
        self.data["sessions"][3]["parentID"] = "ses_root002"
        packet, _ = self.invoke("--json", "--max", "100")
        self.assertGreater(packet["unresolved_parent_records"], 0)
        row = next(r for r in records(packet) if r["id"] == "ses_root001")
        self.assertEqual(
            {r["id"] for r in row["children"]}, {"ses_child000", "ses_child001"}
        )

    def test_native_subpath_is_work_directory_not_worktree_proof(self):
        self.data["sessions"][0]["subpath"] = "nested"
        packet, _ = self.invoke("--json")
        row = next(r for r in records(packet) if r["id"] == "ses_root000")
        self.assertEqual(row["working_directory"], "/projects/0/same-name/nested")
        self.assertIsNone(row["worktree"])

    def test_claude_definitions_are_not_sessions_and_no_all_history(self):
        self.data["claude"] = [
            {
                "id": "definition_fixture",
                "name": "coder",
                "description": "Configured agent, not a session",
            }
        ]
        packet, _ = self.invoke("--json")
        self.assertEqual(coverage(packet, "claude")["status"], "unavailable")
        self.assertEqual(
            [args for v, args, _ in self.calls() if v == "claude"],
            [["agents", "--json"]],
        )
        self.assertFalse(any(r["source"] == "claude" for r in records(packet)))

    def test_human_counts_age_actions_and_reported_states_do_not_promote_child_execution(
        self,
    ):
        self.data["sessions"] = [
            deepcopy(self.data["sessions"][64]),
            deepcopy(self.data["sessions"][154]),
        ]
        root = self.data["sessions"][0]
        root["title"] = "Idle parent"
        old = int(time.time() * 1000) - 2 * 3_600_000
        root["time"].update(created=old, busy=old, idle=old)
        self.data["claude"] = [
            {
                "id": "claude_fixture",
                "kind": "background",
                "state": "working",
                "name": "Claude fixture",
            }
        ]
        self.data["snapshots"] = {"local": self.snapshot(sid="ses_unmatched")}
        packet, _ = self.invoke("--json")
        self.assertEqual(packet["attention_counts"]["native_running"], 1)
        self.assertEqual(packet["attention_counts"]["native_running_children"], 1)
        self.assertEqual(packet["attention_counts"]["herdr_blocked_ui"], 1)
        self.assertEqual(packet["attention_counts"]["claude_reported_working"], 1)
        output, _ = self.invoke(decode=False)
        self.assertIn("not unique agents", output)
        root_line = next(line for line in output.splitlines() if "Idle parent" in line)
        self.assertIn("last-active 2h", root_line)
        self.assertIn("run succeeded; kids 1 running", root_line)
        self.assertNotIn("run running", root_line)
        self.assertIn("show children", root_line)
        self.assertIn("reported working", output)
        self.assertIn("inspect Claude client", output)
        self.assertIn("last-active ?", output)
        self.assertIn("inspect/answer Herdr UI", output)

    def snapshot(self, sid="ses_root001", state="blocked"):
        return {
            "result": {
                "snapshot": {
                    "workspaces": [{"workspace_id": "w1", "label": "same-name"}],
                    "agents": [
                        {
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                            "agent": "opencode",
                            "agent_status": state,
                            "cwd": "/projects/1/same-name",
                            "tokens": {"last_response": "NEVER_REPLY"},
                            "agent_session": {
                                "agent": "opencode",
                                "kind": "id",
                                "source": "herdr:opencode",
                                "value": sid,
                            },
                        }
                    ],
                }
            }
        }

    def test_herdr_blocked_metadata_correlates_only_exact_id_and_machine(self):
        self.data["snapshots"] = {"local": self.snapshot(), "remote": self.snapshot()}
        self.data["machines"] = [{"id": "remote", "enabled": True}]
        packet, _ = self.invoke("--json", "--all-machines", "--max", "100")
        local = next(
            r
            for r in records(packet)
            if r["source"] == "herdr" and r["machine"] == "local"
        )
        remote = next(
            r
            for r in records(packet)
            if r["source"] == "herdr" and r["machine"] == "remote"
        )
        native = next(r for r in records(packet) if r["id"] == "ses_root001")
        self.assertEqual(local["correlated_identity"], native["identity"])
        self.assertIsNone(remote["correlated_identity"])
        self.assertNotEqual(local["identity"], remote["identity"])
        self.assertIn("blocked_ui", local["attention"])
        self.assertEqual(local["execution_state"], "unknown")
        self.assertNotIn("NEVER_REPLY", json.dumps(packet))

    def test_herdr_out_of_context_makes_no_calls_and_does_not_infer_absence(self):
        self.environment.pop("HERDR_ENV")
        packet, _ = self.invoke("--json", "--all-machines")
        self.assertIn(
            "Herdr unavailable in this execution context.",
            coverage(packet, "herdr")["errors"],
        )
        self.assertFalse(any(v == "herdr" for v, _, _ in self.calls()))

    def test_herdr_opaque_alphanumeric_pane_ids_and_malformed_workspace(self):
        snapshot = self.snapshot()
        snapshot["result"]["snapshot"]["agents"][0]["pane_id"] = "w1Z:pC"
        snapshot["result"]["snapshot"]["workspaces"].append({"workspace_id": []})
        self.data["snapshots"] = {"local": snapshot}
        packet, _ = self.invoke("--json", "--max", "100")
        self.assertEqual(coverage(packet, "herdr")["status"], "observed")
        self.assertTrue(any(r["id"] == "w1Z:pC" for r in records(packet)))

    def test_unreachable_remote_does_not_stall_local_or_reuse_stale_data(self):
        self.data["machines"] = [{"id": "remote", "enabled": True}]
        self.data["unreachable"] = ["remote"]
        self.data["snapshots"] = {"remote": self.snapshot(state="done")}
        packet, elapsed = self.invoke("--json", "--all-machines")
        self.assertLess(elapsed, 2.5)
        self.assertEqual(coverage(packet, "herdr", "remote")["status"], "unavailable")
        self.assertFalse(any(r["machine"] == "remote" for r in records(packet)))

    def test_machine_cap_and_disabled_profiles(self):
        self.data["machines"] = [
            {"id": f"remote{i}", "enabled": i != 0} for i in range(8)
        ]
        packet, _ = self.invoke("--json", "--all-machines")
        self.assertEqual(coverage(packet, "herdr-machines")["machines_omitted"], 3)
        remote_calls = [
            a for v, a, _ in self.calls() if v == "herdr" and "--machine" in a
        ]
        self.assertEqual(len(remote_calls), 4)
        self.assertFalse(any("remote0" in a for a in remote_calls))

    def test_machine_selector_does_not_inspect_local_sources(self):
        self.invoke("--json", "--machine", "remote")
        self.assertEqual(
            [(v, a) for v, a, _ in self.calls()],
            [("herdr", ["--machine", "remote", "api", "snapshot"])],
        )

    def test_sources_are_concurrent_with_source_specific_timeouts(self):
        self.data["delays"] = {"opencode": 0.15, "claude": 10, "herdr": 10}
        packet, elapsed = self.invoke("--json")
        self.assertLess(elapsed, 2.8)
        self.assertEqual(coverage(packet, "opencode")["status"], "observed")
        self.assertEqual(coverage(packet, "claude")["status"], "unavailable")
        first = {
            v: min(t for name, _, t in self.calls() if name == v)
            for v in ("opencode", "claude", "herdr")
        }
        self.assertLess(max(first.values()) - min(first.values()), 0.3)

    def test_claude_nullable_cli_id_separate_conversation_and_unmatched_cwd(self):
        self.data["claude"] = [
            {
                "id": None,
                "sessionId": "conv_fixture",
                "kind": "interactive",
                "state": "done",
                "cwd": "/projects/1/same-name",
            }
        ]
        self.data["snapshots"] = {"local": self.snapshot(sid="ses_unmatched")}
        packet, _ = self.invoke("--json", "--max", "100")
        row = next(r for r in records(packet) if r["source"] == "claude")
        self.assertIsNone(row["cli_id"])
        self.assertEqual(row["conversation_id"], "conv_fixture")
        self.assertEqual(row["execution_state"], "unknown")
        self.assertIsNone(
            next(r for r in records(packet) if r["source"] == "herdr")[
                "correlated_identity"
            ]
        )

    def test_claude_interactive_null_or_missing_state_remains_observed_unknown(self):
        self.data["claude"] = [
            {"id": None, "sessionId": "conv_missing", "kind": "interactive"},
            {
                "id": None,
                "sessionId": "conv_null",
                "kind": "interactive",
                "state": None,
            },
        ]
        packet, _ = self.invoke("--json", "--max", "100")
        self.assertEqual(coverage(packet, "claude")["status"], "observed")
        sessions = [r for r in records(packet) if r["source"] == "claude"]
        self.assertEqual(len(sessions), 2)
        self.assertEqual({r["reported_state"] for r in sessions}, {"unknown"})
        self.assertEqual({r["execution_state"] for r in sessions}, {"unknown"})

    def test_cleanup_is_explicit_batched_unknown_on_pending_failure_no_archive(self):
        self.data["fail"] = ["opencode api session.permission.list"]
        self.data["session.form.list"] = {"ses_root000": [{"id": "form_fixture"}]}
        packet, _ = self.invoke(
            "--project", "project_0", "--max", "2", operation="cleanup-plan"
        )
        self.assertFalse(packet["archive_authorized"])
        self.assertEqual(packet["next_offset"], 2)
        self.assertGreater(packet["counts"]["remaining"], 0)
        self.assertTrue(
            all(r["pending_counts"]["permission"] is None for r in records(packet))
        )
        form_row = next(r for r in records(packet) if r["id"] == "ses_root000")
        self.assertEqual(form_row["pending_counts"]["form"], 1)
        self.assertEqual(form_row["pending_input"], "present")
        self.assertTrue(form_row["cleanup_status"].startswith("not eligible"))
        self.assertTrue(
            any(r["cleanup_status"].startswith("unknown") for r in records(packet))
        )
        self.assertTrue(all(v == "opencode" for v, _, _ in self.calls()))
        self.assertFalse(any("update" in " ".join(a) for _, a, _ in self.calls()))
        self.invoke(operation="cleanup-plan", code=2, decode=False)

    def test_cleanup_inbox_count_is_pending_per_installed_schema_contract(self):
        self.data["session.inbox.list"] = {
            "ses_root000": [
                {
                    "id": "msg_pending",
                    "type": "user",
                    "delivery": "queue",
                    "payload": {"text": "DO_NOT_EMIT_PAYLOAD"},
                }
            ]
        }
        packet, _ = self.invoke(
            "--project", "project_0", "--max", "2", operation="cleanup-plan"
        )
        row = next(r for r in records(packet) if r["id"] == "ses_root000")
        self.assertEqual(row["pending_counts"]["inbox"], 1)
        self.assertEqual(row["pending_input"], "present")
        self.assertIn(
            "not yet delivered", coverage(packet, "opencode-pending")["inbox_contract"]
        )
        self.assertNotIn("DO_NOT_EMIT_PAYLOAD", json.dumps(packet))


if __name__ == "__main__":
    unittest.main(verbosity=2)
