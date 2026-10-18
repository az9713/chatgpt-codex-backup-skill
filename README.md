# ChatGPT / Codex Backup Skill

Back up **Codex configuration, session history, scripts and skills** to an ordinary
ZIP so you can keep a local archive or migrate to another laptop. Individual
project checkouts are excluded by default.

Despite the historical skill name, the default backup covers local Codex data.
It does not download ChatGPT cloud conversations or recreate a ChatGPT account.

## Quick start

You need Python 3.11 or newer. No pip packages, passwords or encryption setup
are required. Windows is the primary supported and tested platform.

1. Download or clone this repository.
2. Double-click **Run-Backup.cmd** in its folder.
3. Find the ZIP and file receipts in **D:\backups\chatgpt-codex**.

The destination folder is created if missing. Codex and ChatGPT may stay open.
If you do not have a D: drive, use a different destination from PowerShell:

```powershell
python scripts/backup.py backup --allow-live --destination "$env:USERPROFILE\Backups\chatgpt-codex"
```

All commands below run from this repository's folder. The launcher uses its own
location, so it can be double-clicked from Explorer.

## Install as a Codex skill

Clone into the default Codex skills directory:

```powershell
git clone https://github.com/az9713/chatgpt-codex-backup-skill.git "$env:USERPROFILE\.codex\skills\chatgpt-codex-backup"
```

If `CODEX_HOME` is configured, put the folder inside its `skills` directory
instead. The installed folder should contain `SKILL.md` directly. Start a new
Codex chat after installation, then invoke:

```text
$chatgpt-codex-backup
```

You can also ask: "Use $chatgpt-codex-backup to back up my Codex settings and
session history."

## What is included

The script discovers the current user's `CODEX_HOME` (normally `~/.codex`) and
shared `~/.agents/skills`; it does not contain a hard-coded username or home path.

| Included by default | Examples |
| --- | --- |
| Configuration and login files | `config.toml`, `auth.json`, global settings, instructions |
| Session history | `sessions`, `archived_sessions`, history and session indexes |
| State/history databases | SQLite snapshots of the selected state, thread-history and memory databases |
| Custom behavior | scripts, skills, agents, hooks, rules, prompts and automations |
| Supporting state | memories, selected plugin settings and shared skill definitions |

Project folders, Codex-managed `worktrees`, `.chatgpt-projects`, generated media
and visualizations, app binaries, IDE extensions, plugin download caches, logs,
temporary files and browser profiles are excluded. External MCP repositories
and external link targets are not collected automatically. Historical project
paths remain in transcripts as text; those projects need not be migrated.

Selection is an allowlist in `excluded_subtrees()` in `scripts/backup.py`.
New Codex storage layouts may require updating that allowlist. Optional inventory
flags can explicitly add extra roots or projects; normal backup and skill use do
not add them.

## Backup files and receipts

Each run creates unique filenames:

```text
chatgpt-codex-TIMESTAMP.zip
chatgpt-codex-TIMESTAMP.receipt.txt
chatgpt-codex-TIMESTAMP.receipt.json
```

The readable and JSON receipts list captured files, folders and link metadata,
original and archive paths, sizes, SHA-256 hashes, intentional exclusions and any
skipped sources. The ZIP also includes `manifest.json` and `RESTORE-MAP.json`.
Existing backups are never overwritten.

The script verifies ZIP integrity, payload membership and hashes, then checks
the published archive's readback hash. SQLite databases use individual consistent
snapshots, incorporating committed WAL data without copying redundant sidecars.

A live backup is best-effort across files because Codex can keep changing them.
Locked or unreadable sources are skipped and listed in the receipts. Review those
entries before relying on the backup for migration.

| Python CLI exit code | Meaning |
| --- | --- |
| `0` | Verified backup with no recorded coverage/consistency issues |
| `2` | Verified backup saved with issues; live mode always records a consistency note |
| `1` | Error; do not assume a valid backup was produced |
| `130` | Cancelled |

`Run-Backup.cmd` treats exit `2` as a saved backup and points to the receipts.
The PowerShell wrapper preserves the Python exit code.

## Restore on a new laptop

1. Copy the backup ZIP to the new laptop and right-click **Extract All**.
2. Install Codex, then close it while restoring.
3. Copy the extracted `codex-home` contents to the new user's `.codex` folder,
   or the selected `CODEX_HOME`. Copy `shared-agents/skills` into the new
   `.agents/skills` folder when present. Review `RESTORE-MAP.json` for mappings.
   Use fresh folders or preserve existing data before copying.
4. Start Codex, sign in if required, and check settings and session history.

There is no decryption step. Absolute paths in configuration and history are
preserved rather than rewritten. Reinstall applications, runtimes, MCP tools and
plugins separately as needed. Windows-protected credentials may require signing
in again. Recorded links need review; standard extraction does not recreate them.
A verified ZIP does not prove that every chat will reopen on a new laptop;
check the destination before retiring the old machine.

See [the migration reference](references/migration.md) for staged recovery and the
optional `restore.ps1` helper, which previews by default and refuses existing
targets.

## Optional commands

Inventory only; no backup payload is created:

```powershell
python scripts/backup.py inventory --plan-out codex-plan.json --scan
```

Verify a completed archive and any sibling receipts:

```powershell
python scripts/backup.py verify 'D:\backups\chatgpt-codex\BACKUP.zip'
```

Preview extraction into a fresh staging folder; add `--apply` to extract:

```powershell
python scripts/backup.py restore 'D:\backups\chatgpt-codex\BACKUP.zip' --destination "$env:USERPROFILE\codex-restore-staging"
```

The Python engine supports an explicit destination on other operating systems,
but the macOS/Linux workflows have not been verified here. The `.cmd` and
PowerShell restore helper are for Windows.

## Privacy

This repository contains reusable source and synthetic tests, with no personal
backups, session transcripts, account exports, login credentials or machine
inventories. Its `.gitignore` allows only the reviewed source/documentation paths.
It makes no network requests to upload backup data.

**Generated backups are private data.** They can contain credentials, conversation
content and personal paths. Receipts also reveal file paths and metadata. Keep
ZIPs, receipts and generated plans outside public repositories; they are not
scrubbed or encrypted. Git ignore rules do not prevent an intentional force-add.

## Test

The test suite uses temporary synthetic profiles rather than your live Codex data:

```powershell
python -m unittest discover -s scripts -p test_backup.py -v
```

It covers backup/restore round trips, SQLite WAL snapshots, receipts, project
exclusions, source-read failures, ZIP path validation and Windows extraction and
restore helpers. This is an independent utility, not an official OpenAI migration
service.
