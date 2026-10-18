---
name: chatgpt-codex-backup
description: Back up Codex configuration, session transcripts, history databases and user scripts/skills to a normal ZIP with file receipts. Excludes individual projects and app binaries.
---

# Codex configuration and session backup

Default scope is Codex configuration and session history, independent of project files.
Use the bundled Python 3.11+ script; it needs no third-party packages.

Run `python scripts/backup.py backup --allow-live` from this skill's directory.
Run it yourself when invoked; do not ask the user to run a separate command.
The script creates `D:\backups\chatgpt-codex` if needed and writes a timestamped
ZIP plus `.receipt.txt` and `.receipt.json` listing every included file/folder,
original path, archive path, size, hash, exclusions and skipped files.
Use `--destination` only when the user requests another folder.

Include the active `CODEX_HOME` (normally `~/.codex`) configuration/auth files,
active and archived session transcripts, history/index files, state/history
SQLite snapshots, memories, scripts, skills, agents, hooks, rules, prompts,
automations and plugin settings. Include shared `~/.agents/skills` definitions.
The allowlist is implemented by `excluded_subtrees()` in `scripts/backup.py`.
Do not add external MCP repositories or project folders automatically.

Exclude project checkouts (`worktrees`, `.chatgpt-projects`), generated
visualizations/media, app installations, IDE extensions, plugin download caches,
logs, temporary files and browser profiles. Paths mentioned inside session history
are retained as text; their project directories are never copied automatically.

Keep Codex and ChatGPT open. Live backups copy readable files, snapshot SQLite
and list locked/unreadable sources in the receipt. Never terminate the apps.
Verify CRC, file hashes and archive readback before reporting success. Exit code 2
means a verified ZIP was saved with live-consistency or coverage issues; inspect
and report those issues, rather than treating it as no backup.

Use ordinary ZIP files, no password, encryption or custom archive format.
Keep the ZIP private because configuration can contain credentials.
Report the archive and readable receipt paths, size, counts and any skipped data.

For recovery, right-click the ZIP and choose Extract All. `RESTORE-MAP.json`
records original locations. On a new laptop install Codex, close it for the
restore, then copy selected backed-up files to the corresponding Codex folders.
Sign in again if required. Never overwrite existing profiles without authorization.
See [recovery details](references/migration.md) for optional scripted verification
and staged restore. Do not promise migration of cloud-only ChatGPT history.
