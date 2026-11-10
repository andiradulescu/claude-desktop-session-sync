# claude-desktop-session-sync

Makes your Claude Code CLI conversations show up in the Code tab of the Claude desktop app, in every account and organization you use there.

The desktop app imports CLI transcripts from `~/.claude/projects` only into the account and organization it is logged in with, and it can skip some of them. This script fills the gaps: every conversation is listed in every account/organization workspace.

## How it works

- **Conversations** are the CLI transcripts in `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
- **App registry:** the desktop app keeps one `local_*.json` per session in `~/Library/Application Support/Claude/claude-code-sessions/<account>/<organization>/`.
- **Target workspaces** are the `<account>/<organization>` folders that already hold a session, plus every account/organization that a transcript records as its owner (`session-owner` and `bridge-session` lines), as long as that folder exists.
- **What gets synced:** every transcript with at least one user message that was not started through the Agent SDK (`claude -p` and other scripted runs). Each one gets a registry entry in every target workspace that lacks it:
  - If another account already has an entry, it is copied. The title, archive state and session state are kept; fields tied to the source account (cloud bridges, MCP tool grants, computer-use grants, permission approvals) are dropped.
  - Otherwise the entry is built from the transcript: title, model, permission mode, timestamps, and the working directory that matches the project folder.
- **Safety:** only new files are written. Nothing existing is modified or deleted.

## Usage

```bash
python3 sync_app_sessions.py --dry-run   # show what would be added
python3 sync_app_sessions.py             # write the entries
```

The app reads its registry at launch, so quit and reopen Claude afterwards. To see a workspace, log in to the app with that account and organization.

Running it again is safe: entries that already exist are skipped.

## Requirements

- macOS with the Claude desktop app
- Python 3.9+ (standard library only)

## Tests

```bash
python3 test_sync_app_sessions.py
```

The tests run the script against temporary directories, set through `CLAUDE_APP_SESSIONS_DIR` and `CLAUDE_PROJECTS_DIR`.

## Caveat

The app's registry is an internal, undocumented format and may change between app versions. Run `--dry-run` first after an app update.

## License

MIT
