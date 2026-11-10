#!/usr/bin/env python3
"""Make every Claude Code conversation visible in every account of the Claude desktop app.

Sources of conversations:
  - CLI transcripts:  ~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl
  - App registries:   ~/Library/Application Support/Claude/claude-code-sessions/<account>/<workspace>/local_*.json

Target workspaces (<account>/<organization> folders) are the ones that already have a session,
plus every account/organization recorded as an owner in a transcript (session-owner and
bridge-session lines) whose folder exists. Every conversation with a transcript on disk, at
least one user message, and not started through the SDK (scripted `claude -p` runs) gets a
registry entry in each target workspace. Entries that exist in another account are copied
(keeping title, archive state and session state); CLI-only conversations get an entry built
from the transcript. Fields tied to the source account (cloud bridges, MCP tool grants,
computer-use grants, permission approvals) are not carried over; the target workspace's remote
MCP config is used when it has one.

Only new files are written. Nothing existing is modified or deleted.
The app reads its registry at launch, so quit and reopen Claude after syncing.

Usage: sync_app_sessions.py [--dry-run]
Env overrides (used by tests): CLAUDE_APP_SESSIONS_DIR, CLAUDE_PROJECTS_DIR
"""
import argparse
import datetime
import glob
import json
import os
import sys
import uuid

APP_SESSIONS_DIR = os.environ.get("CLAUDE_APP_SESSIONS_DIR", os.path.expanduser(
    "~/Library/Application Support/Claude/claude-code-sessions"))
PROJECTS_DIR = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))

# Fields bound to the account that created the entry; never copied to another account.
ACCOUNT_BOUND_FIELDS = ("bridgeSessionIds", "enabledMcpTools", "cuGrantFlags", "cuAllowedApps",
                        "alwaysAllowedReasons", "sessionPermissionUpdates", "remoteMcpServersConfig")

TITLE_MAX_CHARS = 60

# Entrypoint written by the Agent SDK / `claude -p`; these are scripted runs, not conversations.
SDK_ENTRYPOINT = "sdk-cli"


def warn(msg):
    print("warning: " + msg, file=sys.stderr)


def encode_project_dir(path):
    """Folder name Claude Code uses under ~/.claude/projects for a working directory."""
    return "".join(c if c.isalnum() else "-" for c in path)


class Workspace:
    def __init__(self, path, entries):
        self.path = path
        self.entries = entries
        self.cli_ids = {e["cliSessionId"] for e in entries}
        with_mcp = [e for e in entries if "remoteMcpServersConfig" in e]
        self.mcp_template = max(with_mcp, key=lambda e: e.get("lastActivityAt", 0)) if with_mcp else None

    @property
    def label(self):
        account, workspace = self.path.rstrip("/").split("/")[-2:]
        return "%s/%s" % (account[:8], workspace[:8])


def load_workspaces(app_dir, owners):
    """Workspaces that have a session, plus the owner (account, organization) folders that exist."""
    owner_dirs = set()
    for account, organization in sorted(owners):
        ws_dir = os.path.join(app_dir, account, organization, "")
        if os.path.isdir(ws_dir):
            owner_dirs.add(ws_dir)
        else:
            warn("no app folder for owner %s/%s, open a Code session with that account and "
                 "organization first" % (account[:8], organization[:8]))
    workspaces = []
    for ws_dir in sorted(glob.glob(os.path.join(app_dir, "*", "*", ""))):
        entries = []
        for f in sorted(glob.glob(os.path.join(ws_dir, "local_*.json"))):
            try:
                with open(f) as fh:
                    entry = json.load(fh)
            except (OSError, ValueError) as e:
                warn("skipping unreadable registry file %s: %s" % (f, e))
                continue
            if isinstance(entry, dict) and entry.get("cliSessionId"):
                entries.append(entry)
        if entries or ws_dir in owner_dirs:
            workspaces.append(Workspace(ws_dir, entries))
    return workspaces


def parse_timestamp_ms(value):
    return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def user_text(content):
    """Text of a user message, from a plain string or the first text block of a content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
    return None


def read_transcript(path):
    """Return metadata for a transcript, or None if it is not a conversation to sync."""
    info = {"cwds": [], "custom_title": None, "auto_title": None, "first_message": None,
            "user_turns": 0, "created": None, "last": None, "entrypoint": None, "model": None,
            "permission_mode": None, "owners": set()}
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                o = json.loads(line)
            except ValueError:
                warn("%s:%d is not valid JSON, ignoring that line" % (path, n))
                continue
            if not isinstance(o, dict):
                continue
            kind = o.get("type")
            if o.get("cwd") and o["cwd"] not in info["cwds"]:
                info["cwds"].append(o["cwd"])
            info["entrypoint"] = info["entrypoint"] or o.get("entrypoint")
            if o.get("timestamp"):
                try:
                    t = parse_timestamp_ms(o["timestamp"])
                except ValueError:
                    t = None
                if t is not None:
                    info["created"] = info["created"] or t
                    info["last"] = t
            if kind == "custom-title":
                info["custom_title"] = o.get("customTitle") or info["custom_title"]
            elif kind in ("ai-title", "summary"):
                info["auto_title"] = o.get("aiTitle") or o.get("summary") or info["auto_title"]
            elif kind == "permission-mode":
                info["permission_mode"] = o.get("permissionMode") or info["permission_mode"]
            elif kind == "session-owner" and o.get("accountUuid") and o.get("organizationUuid"):
                info["owners"].add((o["accountUuid"], o["organizationUuid"]))
            elif kind == "bridge-session" and o.get("ownerAccountUuid") and o.get("ownerOrganizationUuid"):
                info["owners"].add((o["ownerAccountUuid"], o["ownerOrganizationUuid"]))
            elif kind == "assistant":
                model = (o.get("message") or {}).get("model") or ""
                if model.startswith("claude"):
                    info["model"] = model
            elif kind == "user" and not o.get("isSidechain") and not o.get("isMeta"):
                text = user_text((o.get("message") or {}).get("content"))
                if text and not text.startswith("<"):
                    info["user_turns"] += 1
                    info["first_message"] = info["first_message"] or text
    if info["user_turns"] == 0 or not info["cwds"] or info["entrypoint"] == SDK_ENTRYPOINT:
        return None
    # Resume looks the transcript up by the session's working directory, so prefer the cwd
    # whose encoding is the project folder this transcript is stored in.
    project_folder = os.path.basename(os.path.dirname(path))
    info["cwd"] = next((c for c in info["cwds"] if encode_project_dir(c) == project_folder), info["cwds"][0])
    if info["created"] is None:
        info["created"] = info["last"] = int(os.path.getmtime(path) * 1000)
    title = info["custom_title"] or info["auto_title"] or info["first_message"]
    info["title"] = " ".join(title.split())[:TITLE_MAX_CHARS]
    return info


def load_transcripts(projects_dir):
    transcripts = {}
    for path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))):
        info = read_transcript(path)
        if info:
            transcripts[os.path.basename(path)[:-len(".jsonl")]] = info
    return transcripts


def entry_from_other_account(source, info, target):
    entry = {k: v for k, v in source.items() if k not in ACCOUNT_BOUND_FIELDS}
    if not entry.get("title"):
        entry["title"] = info["title"]
    return finalize(entry, target)


def entry_from_transcript(cli_id, info, target):
    # Same fields the app writes when it imports a CLI transcript itself.
    entry = {"cliSessionId": cli_id, "cwd": info["cwd"], "originCwd": info["cwd"],
             "createdAt": info["created"], "lastActivityAt": info["last"],
             "isArchived": False, "title": info["title"], "titleSource": "auto",
             "permissionMode": info["permission_mode"] or "default",
             "interruptedAwaitingPerson": None, "steeredByRemoteClient": None,
             "indexedAt": int(datetime.datetime.now().timestamp() * 1000),
             "adoptedFromOtherSurface": True, "fromCliTranscript": True}
    if info["model"]:
        entry["model"] = info["model"]
    return finalize(entry, target)


def finalize(entry, target):
    entry["sessionId"] = "local_" + str(uuid.uuid4())
    if target.mcp_template:
        entry["remoteMcpServersConfig"] = target.mcp_template["remoteMcpServersConfig"]
    entry["bridgeSessionIds"] = []
    entry["alwaysAllowedReasons"] = []
    entry["sessionPermissionUpdates"] = []
    return entry


def plan(workspaces, transcripts):
    """Return [(workspace, entry)] for every conversation missing from a workspace."""
    additions = []
    for target in workspaces:
        for cli_id, info in transcripts.items():
            if cli_id in target.cli_ids:
                continue
            sources = [e for ws in workspaces if ws is not target
                       for e in ws.entries if e.get("cliSessionId") == cli_id]
            if sources:
                source = max(sources, key=lambda e: e.get("lastActivityAt", 0))
                entry = entry_from_other_account(source, info, target)
            else:
                entry = entry_from_transcript(cli_id, info, target)
            additions.append((target, entry))
    return additions


def write_entry(workspace, entry):
    path = os.path.join(workspace.path, entry["sessionId"] + ".json")
    with open(path, "x") as fh:
        json.dump(entry, fh, indent="\t", ensure_ascii=False)
    with open(path) as fh:
        json.load(fh)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would be added, write nothing")
    args = parser.parse_args(argv)

    if not os.path.isdir(APP_SESSIONS_DIR):
        print("error: Claude app sessions dir not found: %s" % APP_SESSIONS_DIR, file=sys.stderr)
        return 1
    if not os.path.isdir(PROJECTS_DIR):
        print("error: Claude Code projects dir not found: %s" % PROJECTS_DIR, file=sys.stderr)
        return 1

    transcripts = load_transcripts(PROJECTS_DIR)
    owners = set().union(*(info["owners"] for info in transcripts.values()))
    workspaces = load_workspaces(APP_SESSIONS_DIR, owners)
    if not workspaces:
        print("error: no app workspace has a session yet. Open one Code session in each account first.",
              file=sys.stderr)
        return 1
    additions = plan(workspaces, transcripts)

    print("Workspaces: " + ", ".join("%s (%d sessions)" % (w.label, len(w.entries)) for w in workspaces))
    if not additions:
        print("Nothing to sync, every conversation is already in every workspace.")
        return 0

    for workspace, entry in additions:
        if not args.dry_run:
            write_entry(workspace, entry)
        print("%s %s + %s | %s | %s" % ("[dry run]" if args.dry_run else "added", workspace.label,
                                         entry["title"], entry["cwd"], entry["cliSessionId"]))
    if args.dry_run:
        print("Dry run: %d entries would be added. Run without --dry-run to write them." % len(additions))
    else:
        print("Added %d entries. Quit and reopen the Claude app to see them." % len(additions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
