import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "sync_app_sessions.py")

PERSONAL = ("acct-personal", "ws-personal")
WORK = ("acct-work", "ws-work")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def write_transcript(projects, cwd, sid, user_messages, title=None, owner=None, entrypoint="cli",
                     project_dir=None, model=None):
    enc = "".join(c if c.isalnum() else "-" for c in (project_dir or cwd))
    path = os.path.join(projects, enc, sid + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [{"type": "permission-mode", "permissionMode": "auto", "sessionId": sid}]
    if owner:
        lines.append({"type": "session-owner", "accountUuid": owner[0], "organizationUuid": owner[1],
                      "sessionId": sid})
    for i, text in enumerate(user_messages):
        lines.append({"type": "user", "cwd": cwd, "sessionId": sid, "entrypoint": entrypoint,
                      "timestamp": "2026-11-0%dT10:00:00.000Z" % (i + 1),
                      "message": {"role": "user", "content": text}})
        if model:
            lines.append({"type": "assistant", "cwd": cwd, "sessionId": sid,
                          "timestamp": "2026-11-0%dT10:00:01.000Z" % (i + 1),
                          "message": {"role": "assistant", "model": model, "content": "ok"}})
    if title:
        lines.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def registry_entry(sid, cli_sid, title, cwd, mcp, **extra):
    entry = {"sessionId": sid, "cliSessionId": cli_sid, "cwd": cwd, "originCwd": cwd,
             "createdAt": 1000, "lastActivityAt": 2000, "lastFocusedAt": 2000,
             "model": "claude-opus-5-5", "isArchived": False, "title": title,
             "titleSource": "auto", "permissionMode": "auto",
             "remoteMcpServersConfig": mcp, "bridgeSessionIds": ["bridge-x"],
             "alwaysAllowedReasons": [], "sessionPermissionUpdates": []}
    entry.update(extra)
    return entry


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = os.path.join(self.tmp.name, "claude-code-sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.projects)
        self.cwd = "/Users/x/Projects/demo"
        write_transcript(self.projects, self.cwd, "seed-p", ["hi"])
        write_transcript(self.projects, self.cwd, "seed-w", ["hi"])
        self.add_entry(PERSONAL, registry_entry("local_p0", "seed-p", "Seed P", self.cwd, ["personal-mcp"]))
        self.add_entry(WORK, registry_entry("local_w0", "seed-w", "Seed W", self.cwd, ["work-mcp"]))

    def tearDown(self):
        self.tmp.cleanup()

    def add_entry(self, ws, entry):
        write_json(os.path.join(self.app, ws[0], ws[1], entry["sessionId"] + ".json"), entry)

    def entries(self, ws):
        d = os.path.join(self.app, ws[0], ws[1])
        out = []
        for name in sorted(os.listdir(d)):
            if name.startswith("local_") and name.endswith(".json"):
                with open(os.path.join(d, name)) as f:
                    out.append(json.load(f))
        return out

    def by_cli(self, ws, cli_sid):
        return [e for e in self.entries(ws) if e.get("cliSessionId") == cli_sid]

    def run_sync(self, *args):
        env = dict(os.environ, CLAUDE_APP_SESSIONS_DIR=self.app, CLAUDE_PROJECTS_DIR=self.projects)
        return subprocess.run([sys.executable, SCRIPT, *args], env=env,
                              capture_output=True, text=True)

    def test_seed_sessions_are_shared_between_accounts(self):
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.by_cli(WORK, "seed-p")), 1)
        self.assertEqual(len(self.by_cli(PERSONAL, "seed-w")), 1)

    def test_cli_only_transcript_is_registered_in_every_account(self):
        write_transcript(self.projects, self.cwd, "cli-only", ["first question", "second"], title="CLI Chat")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        for ws, mcp in ((PERSONAL, ["personal-mcp"]), (WORK, ["work-mcp"])):
            [e] = self.by_cli(ws, "cli-only")
            self.assertEqual(e["title"], "CLI Chat")
            self.assertEqual(e["cwd"], self.cwd)
            self.assertEqual(e["remoteMcpServersConfig"], mcp)
            self.assertEqual(e["bridgeSessionIds"], [])
            self.assertTrue(e["sessionId"].startswith("local_"))
            self.assertLess(e["createdAt"], e["lastActivityAt"])

    def test_title_falls_back_to_first_user_message(self):
        write_transcript(self.projects, self.cwd, "untitled", ["please fix the build"])
        self.run_sync()
        [e] = self.by_cli(WORK, "untitled")
        self.assertEqual(e["title"], "please fix the build")

    def test_entry_copied_from_other_account_keeps_session_state_and_drops_account_bound_fields(self):
        write_transcript(self.projects, self.cwd, "app-chat", ["hello"])
        self.add_entry(PERSONAL, registry_entry(
            "local_p1", "app-chat", "Renamed by me", self.cwd, ["personal-mcp"],
            titleSource="user", isArchived=True, enabledMcpTools={"a": True},
            cuGrantFlags={"x": 1}, cuAllowedApps=["Finder"],
            alwaysAllowedReasons=["r"], sessionPermissionUpdates=["u"]))
        self.run_sync()
        [e] = self.by_cli(WORK, "app-chat")
        self.assertEqual(e["title"], "Renamed by me")
        self.assertEqual(e["titleSource"], "user")
        self.assertTrue(e["isArchived"])
        self.assertEqual(e["remoteMcpServersConfig"], ["work-mcp"])
        self.assertEqual(e["bridgeSessionIds"], [])
        self.assertEqual(e["alwaysAllowedReasons"], [])
        self.assertEqual(e["sessionPermissionUpdates"], [])
        for field in ("enabledMcpTools", "cuGrantFlags", "cuAllowedApps"):
            self.assertNotIn(field, e)
        self.assertNotEqual(e["sessionId"], "local_p1")

    def test_second_run_adds_nothing(self):
        write_transcript(self.projects, self.cwd, "cli-only", ["q"])
        self.run_sync()
        before = (len(self.entries(PERSONAL)), len(self.entries(WORK)))
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((len(self.entries(PERSONAL)), len(self.entries(WORK))), before)
        self.assertIn("Nothing to sync", r.stdout)

    def test_transcript_without_user_messages_is_skipped(self):
        write_transcript(self.projects, self.cwd, "empty", [])
        self.run_sync()
        self.assertEqual(self.by_cli(WORK, "empty"), [])
        self.assertEqual(self.by_cli(PERSONAL, "empty"), [])

    def test_entry_without_transcript_is_not_copied(self):
        self.add_entry(PERSONAL, registry_entry("local_p2", "gone", "Gone", self.cwd, ["personal-mcp"],
                                                transcriptUnavailable=True))
        self.add_entry(PERSONAL, {"sessionId": "local_p3", "title": "No cli id", "cwd": self.cwd,
                                  "remoteMcpServersConfig": [], "transcriptUnavailable": True})
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.by_cli(WORK, "gone"), [])
        self.assertEqual([e for e in self.entries(WORK) if e.get("title") == "No cli id"], [])

    def test_workspace_without_sessions_is_ignored(self):
        empty_ws = os.path.join(self.app, "acct-work", "ws-other")
        write_json(os.path.join(empty_ws, "scheduled-tasks.json"), {"scheduledTasks": []})
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(empty_ws)), ["scheduled-tasks.json"])

    def test_dry_run_writes_nothing(self):
        write_transcript(self.projects, self.cwd, "cli-only", ["q"])
        r = self.run_sync("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.by_cli(WORK, "cli-only"), [])
        self.assertIn("cli-only", r.stdout)
        self.assertIn("dry run", r.stdout.lower())

    def test_unreadable_registry_file_is_reported_and_left_alone(self):
        bad = os.path.join(self.app, "acct-work", "ws-work", "local_bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("local_bad.json", r.stderr)
        with open(bad) as f:
            self.assertEqual(f.read(), "{not json")

    def test_entries_imported_by_the_app_count_as_present(self):
        # The app's own CLI imports have no remoteMcpServersConfig; they must not be duplicated.
        write_transcript(self.projects, self.cwd, "imported", ["q"])
        for ws in (PERSONAL, WORK):
            self.add_entry(ws, {"sessionId": "local_imported", "cliSessionId": "imported", "cwd": self.cwd,
                                "title": "Imported", "fromCliTranscript": True})
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.by_cli(PERSONAL, "imported")), 1)
        self.assertEqual(len(self.by_cli(WORK, "imported")), 1)

    def test_owner_workspace_without_sessions_receives_every_conversation(self):
        other = ("acct-other", "ws-other")
        write_json(os.path.join(self.app, other[0], other[1], "scheduled-tasks.json"), {"scheduledTasks": []})
        write_transcript(self.projects, self.cwd, "owned", ["q"], owner=other)
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        for cli_sid in ("owned", "seed-p", "seed-w"):
            [e] = self.by_cli(other, cli_sid)
            self.assertNotIn("remoteMcpServersConfig", e)
        self.assertEqual(len(self.by_cli(WORK, "owned")), 1)

    def test_owner_without_app_folder_is_reported_and_not_created(self):
        write_transcript(self.projects, self.cwd, "owned", ["q"], owner=("acct-gone", "ws-gone"))
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("acct-gon", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.app, "acct-gone", "ws-gone")))

    def test_sdk_transcripts_are_skipped(self):
        write_transcript(self.projects, self.cwd, "scripted", ["Reply with OK"], entrypoint="sdk-cli")
        self.run_sync()
        self.assertEqual(self.by_cli(WORK, "scripted"), [])

    def test_cwd_is_the_project_folder_the_transcript_lives_in(self):
        path = write_transcript(self.projects, self.cwd + "/sub", "moved", ["q"], project_dir=self.cwd)
        with open(path, "a") as f:
            f.write(json.dumps({"type": "user", "cwd": self.cwd, "sessionId": "moved",
                                "timestamp": "2026-11-09T10:00:00.000Z",
                                "message": {"role": "user", "content": "back in the project"}}) + "\n")
        self.run_sync()
        [e] = self.by_cli(WORK, "moved")
        self.assertEqual(e["cwd"], self.cwd)

    def test_cli_only_entry_takes_model_from_transcript_not_from_other_sessions(self):
        self.add_entry(WORK, registry_entry("local_w9", "seed-w9", "Other", self.cwd, ["work-mcp"],
                                            model="claude-other", pendingSystemReminder="stale",
                                            lastActivityAt=9999))
        write_transcript(self.projects, self.cwd, "seed-w9", ["hi"])
        write_transcript(self.projects, self.cwd, "modelled", ["q"], model="claude-opus-5-5")
        self.run_sync()
        [e] = self.by_cli(WORK, "modelled")
        self.assertEqual(e["model"], "claude-opus-5-5")
        self.assertNotIn("pendingSystemReminder", e)

    def test_copied_entry_without_title_gets_title_from_transcript(self):
        write_transcript(self.projects, self.cwd, "no-title", ["fix the login page"])
        entry = registry_entry("local_p4", "no-title", None, self.cwd, ["personal-mcp"])
        del entry["title"]
        self.add_entry(PERSONAL, entry)
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        [e] = self.by_cli(WORK, "no-title")
        self.assertEqual(e["title"], "fix the login page")

    def test_custom_title_wins_over_later_ai_title(self):
        path = write_transcript(self.projects, self.cwd, "titled", ["q"], title="AI title")
        with open(path, "a") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": "Mine", "sessionId": "titled"}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Later AI", "sessionId": "titled"}) + "\n")
        self.run_sync()
        [e] = self.by_cli(WORK, "titled")
        self.assertEqual(e["title"], "Mine")

    def test_missing_app_dir_fails_with_message(self):
        env = dict(os.environ, CLAUDE_APP_SESSIONS_DIR=os.path.join(self.tmp.name, "nope"),
                   CLAUDE_PROJECTS_DIR=self.projects)
        r = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nope", r.stderr)


if __name__ == "__main__":
    unittest.main()
