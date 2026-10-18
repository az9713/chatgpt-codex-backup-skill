# Codex backup and recovery

Double-click `Run-Backup.cmd` in the skill folder, or invoke
`$chatgpt-codex-backup` in Codex. No project selection or password is needed.
The default destination is `D:\backups\chatgpt-codex`, created automatically.
Use `--destination` for another folder. Apps can stay open during backup.

The default collects Codex settings, credentials, active/archived session
transcripts, indexes, history/state databases, memories and user scripts/skills.
It omits project checkouts, generated media/apps, app binaries, caches and logs.
External MCP server source repositories are not copied. Settings can still refer
to old project paths; preserving those references does not copy project files.
ChatGPT cloud history is outside this local Codex backup.

Each backup produces a ZIP, a readable `.receipt.txt` and a `.receipt.json`.
The receipt lists all captured files/folders with hashes, original locations,
excluded paths and unreadable sources. `manifest.json` and `RESTORE-MAP.json`
are also inside the ZIP. Existing backups are never overwritten.
Live mode is marked best-effort because files can change at different times;
SQLite uses individual consistent snapshots. Exit 2 is a saved verified ZIP
with issues recorded in the receipt, not a failed archive creation.

## One command

```powershell
python scripts/backup.py backup --allow-live
```

Optional inventory (metadata only):

```powershell
python scripts/backup.py inventory --plan-out codex-plan.json --scan
```

An existing saved plan can be supplied with `--plan`, but the normal launcher
rediscovers the narrow Codex scope on each run, so it cannot reuse an old broad plan.
Python 3.11+ standard library is sufficient; there is no pip install.

## Recover on another laptop

1. Copy the ZIP to the new laptop and right-click **Extract All**.
2. Install Codex, then close it while restoring files.
3. Copy the contents of `codex-home` to your new `%USERPROFILE%\.codex`
   (or the configured `CODEX_HOME`). Copy `shared-agents/skills` to
   `%USERPROFILE%\.agents\skills` if present. Review `RESTORE-MAP.json`
   for original source paths; use fresh folders or preserve existing files first.
4. Start Codex, sign in if needed and check settings and session history.
   Review old absolute paths and reinstall needed runtimes/MCP tools separately.

No decryption is needed. Windows-protected credentials can require sign-in again.
Directory links are recorded as metadata; external link target contents are not
implicitly included. Receipts make such coverage gaps visible.

Optional verification: `python scripts/backup.py verify BACKUP.zip`.
Optional staged restore: `python scripts/backup.py restore BACKUP.zip --destination FRESH_FOLDER --apply`.
The existing `restore.ps1` helper previews promotion by default; use it only when
scripted target mapping is useful. A migration is not verified until Codex has
been checked on the destination laptop.
