#!/usr/bin/env python3
"""Standard ZIP ChatGPT/Codex migration backups. Python 3.11+, standard library only.

No network calls, passwords, encryption or third-party dependencies.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

DEFAULT_DESTINATION = r"D:\backups\chatgpt-codex"
LIMITS = [
    "Cloud-only ChatGPT/Codex data is not downloaded by this script.",
    "OS keychains, browser profiles outside selected roots, WSL, registries, installed apps and system environment values are not exported.",
    "Reinstall applications/runtimes, sign in and reconnect services on the new computer.",
    "Absolute paths in settings, databases and Git worktree pointers need migration review; they are not rewritten.",
    "Filesystem ACLs, alternate data streams, hardlink identity and registry app settings are not preserved.",
]


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=True)
        f.write("\n")


def under(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def linklike(path):
    s = path.lstat()
    # AF_UNIX sockets are reparse points too, but are not filesystem links.
    return stat.S_ISLNK(s.st_mode) or getattr(s, "st_reparse_tag", 0) in (0xA0000003, 0xA000000C)


def safe_relative(name):
    p = PurePosixPath(name)
    # Also block Windows ADS, drive/UNC paths, ambiguous names and device files.
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (not name or p.is_absolute() or "\\" in name or ":" in name
            or any(c in name for c in "\x00\r\n")
            or any(x in ("", ".", "..") or x.endswith((" ", "."))
                   or x.split(".")[0].upper() in reserved for x in name.split("/"))):
        raise ValueError("Unsafe archive path")
    return p


def check_destination(path):
    """Reject symlink/junction ancestors, including the final destination."""
    p = Path(os.path.abspath(path))
    for item in [p, *p.parents]:
        if os.path.lexists(item) and (linklike(item) or bool(getattr(item.lstat(), "st_file_attributes", 0) & 0x400)):
            raise ValueError("Destination contains a symlink or junction")
    return p


def restrict_dir(p, is_directory=True):
    """Set private permissions before any plaintext is written."""
    if os.name == "nt":
        r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        sid = re.search(r"S-1-\d+(?:-\d+)+", r.stdout)
        if not sid:
            raise RuntimeError("Cannot identify current Windows SID")
        grant = f"*{sid[0]}:" + ("(OI)(CI)F" if is_directory else "F")
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", grant], capture_output=True, check=True)
    else:
        p.chmod(0o700 if is_directory else 0o600)


def choose_output(output=None, destination=None):
    if output is not None:
        return check_destination(output)
    if destination is None:
        if os.name != "nt":
            raise ValueError("Supply --destination on this OS; the saved default is a Windows drive path")
        destination = DEFAULT_DESTINATION
    folder = check_destination(destination)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise ValueError("Backup destination must be a directory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return folder / f"chatgpt-codex-{stamp}.zip"


def sha256_file(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            sha.update(data)
    return sha.hexdigest()


def receipt_paths(output):
    output = Path(output)
    return (output.with_suffix(".receipt.json"), output.with_suffix(".receipt.txt"))


def build_receipt(output, manifest, archive_sha256):
    roots = {r["label"]: r for r in manifest["plan"]["roots"]}
    included = []
    for entry in manifest["entries"]:
        label, _, relative = entry["name"].partition("/")
        root = roots[label]
        source = Path(root["source"])
        if root.get("is_directory", True) and relative:
            source = source.joinpath(*PurePosixPath(relative).parts)
        capture = "sqlite-snapshot" if entry.get("sqlite_snapshot") else "link-metadata-only" if entry["type"] == "link" else entry["type"]
        included.append({**entry, "source_path": str(source), "capture": capture})
    return {"schema": 1, "archive": Path(output).name, "archive_bytes": Path(output).stat().st_size,
            "archive_sha256": archive_sha256, "created_utc": manifest["created_utc"], "status": manifest["status"],
            "scope": manifest["plan"].get("scope", "selected-local-roots"), "roots": list(roots.values()),
            "archive_metadata_files": [{"name": "manifest.json", "purpose": "Canonical capture manifest"},
                                       {"name": "RESTORE-MAP.json", "purpose": "Migration source/destination mapping"}],
            "counts": {"files": sum(e["type"] == "file" for e in included),
                       "folders": sum(e["type"] == "directory" for e in included),
                       "links": sum(e["type"] == "link" for e in included),
                       "payload_bytes": sum(e.get("size", 0) for e in included)},
            "verification": {"zip_integrity": "passed", "payload_hashes": "passed", "published_archive_readback_sha256": "passed"},
            "included": included, "intentional_exclusions": manifest.get("intentional_exclusions", []),
            "runtime_artifacts_not_copied": manifest.get("runtime_artifacts_not_copied", []),
            "issues": manifest["issues"], "limitations": manifest["limitations"]}


def format_receipt(receipt):
    lines = ["ChatGPT / Codex backup receipt", "Archive: " + receipt["archive"],
             "UTC: " + receipt["created_utc"], "Status: " + receipt["status"],
             "Archive SHA-256: " + receipt["archive_sha256"],
             "Counts: " + json.dumps(receipt["counts"], sort_keys=True),
             "Verification: " + json.dumps(receipt["verification"], sort_keys=True),
             "", "ROOTS (original source paths)"]
    lines.extend(json.dumps(root, ensure_ascii=True, sort_keys=True) for root in receipt["roots"])
    lines.extend(["", "ARCHIVE METADATA FILES"])
    lines.extend(json.dumps(item, sort_keys=True) for item in receipt["archive_metadata_files"])
    lines.extend(["", "INCLUDED: capture, source_path, archive_path, bytes, SHA-256"])
    for entry in receipt["included"]:
        lines.append("\t".join([entry["capture"], json.dumps(entry["source_path"], ensure_ascii=True),
                                json.dumps(entry["name"], ensure_ascii=True), str(entry.get("size", "")), entry.get("sha256", "")]))
        if entry["type"] == "link":
            lines.append("  Recorded link target: " + json.dumps(entry["target"], ensure_ascii=True))
    for heading, key in [("INTENTIONAL EXCLUSIONS", "intentional_exclusions"),
                         ("RUNTIME ARTIFACTS NOT COPIED", "runtime_artifacts_not_copied"),
                         ("COVERAGE / CONSISTENCY ISSUES", "issues"), ("LIMITATIONS", "limitations")]:
        lines.extend(["", heading])
        lines.extend(json.dumps(value, ensure_ascii=True, sort_keys=True) for value in receipt[key])
    lines.extend(["", "SQLite snapshots incorporate committed WAL data; redundant WAL/SHM/journal files are not copied separately.",
                  "Link entries preserve metadata; an external link target is not implicitly backed up.",
                  "This receipt lists metadata only. The ZIP contains the captured file contents and credentials."])
    return "\n".join(lines) + "\n"


def write_receipts(output, receipt):
    paths = receipt_paths(output)
    contents = [json.dumps(receipt, ensure_ascii=True, indent=2) + "\n", format_receipt(receipt)]
    for path, content in zip(paths, contents):
        check_destination(path)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            restrict_dir(path, is_directory=False)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return {"json": str(paths[0]), "text": str(paths[1])}


def verify_receipts(output, manifest):
    paths = receipt_paths(output)
    if not any(p.exists() for p in paths):
        return {"json": "not-present", "text": "not-present"}
    expected = build_receipt(output, manifest, sha256_file(output))
    result = {}
    for kind, path in zip(("json", "text"), paths):
        if not path.exists():
            result[kind] = "not-present"
            continue
        actual = path.read_text(encoding="utf-8")
        if (json.loads(actual) != expected if kind == "json" else actual != format_receipt(expected)):
            raise ValueError("Backup receipt does not match the verified archive")
        result[kind] = "verified"
    return result


def private_dir(parent=None):
    p = Path(tempfile.mkdtemp(prefix="codex-backup-", dir=parent))
    try:
        restrict_dir(p)
        return p
    except BaseException:
        p.rmdir()
        raise


@contextlib.contextmanager
def scratch(parent=None):
    p = private_dir(parent)
    try:
        yield p
    finally:
        # Only delete this freshly-created private temporary directory.
        shutil.rmtree(p)


def running_apps():
    if os.name == "nt":
        script = "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|ChatGPT|chatgpt-work)$' } | Select-Object -ExpandProperty ProcessName"
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=25)
        if r.returncode:
            raise RuntimeError("Could not check app processes")
        return sorted(set(r.stdout.split()))
    r = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, check=True, timeout=10)
    return sorted({Path(x.strip()).name for x in r.stdout.splitlines() if Path(x.strip()).name.lower() in {"codex", "chatgpt", "chatgpt-work"}})


def runtime_inventory():
    tools = {name: shutil.which(name) for name in ("python", "node", "npm", "git", "uv", "codex", "claude", "ffmpeg", "pwsh")}
    versions = {}
    # Never invoke Codex or third-party agents while preparing a quiescent backup.
    for name in ("node", "git", "uv"):
        if tools[name]:
            try:
                r = subprocess.run([tools[name], "--version"], capture_output=True, text=True, timeout=10)
                versions[name] = (r.stdout or r.stderr).strip()[:300]
            except (OSError, subprocess.TimeoutExpired):
                versions[name] = "unavailable"
    apps = []
    if os.name == "nt":
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "@(Get-AppxPackage | Where-Object { $_.Name -match 'OpenAI|ChatGPT|Codex' } | Select-Object Name,Version,PackageFamilyName) | ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=35, check=True)
            apps = json.loads(r.stdout) if r.stdout.strip() else []
        except (OSError, ValueError, subprocess.SubprocessError):
            apps = [{"inventory_error": "Windows app inventory unavailable"}]
    return {"os": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "tools": tools, "versions": versions, "windows_apps": apps}


def discover(profile=None):
    """Only Codex configuration, transcripts and user scripts; no app installs."""
    home = Path(profile).absolute() if profile else Path.home()
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")) if not profile else home / ".codex"
    roots = []
    for label, path, kind in [("codex-home", codex, "codex"), ("shared-agents", home / ".agents", "skills")]:
        if path.exists():
            path = path.absolute()
            roots.append({"label": label, "source": str(path), "kind": kind,
                          "home_relative": str(path.relative_to(home)) if under(path, home) else None,
                          "is_directory": True})
    return {"schema": 1, "profile": str(home), "roots": roots, "limitations": LIMITS,
            "scope": "codex-config-sessions", "include_managed_worktrees": False}


def add_root(plan, path, kind):
    p = Path(path).expanduser().absolute()
    if not p.exists() or linklike(p):
        raise ValueError("An explicitly selected root must exist and must not be a link")
    if any(under(p.resolve(), Path(x["source"]).resolve()) or under(Path(x["source"]).resolve(), p.resolve()) for x in plan["roots"]):
        raise ValueError("Selected roots overlap; select each directory tree once")
    home = Path(plan["profile"])
    plan["roots"].append({"label": f"{kind}-{len(plan['roots'])}", "source": str(p), "kind": kind,
                          "home_relative": str(p.relative_to(home)) if under(p, home) else None, "is_directory": p.is_dir()})
    if kind == "project":
        plan["scope"] = "app-and-selected-projects"


def excluded_subtrees(plan, root):
    """Whitelist durable Codex state, excluding project and runtime directories."""
    if plan.get("scope") == "codex-config-sessions":
        base = Path(root["source"])
        if root.get("kind") == "codex":
            folders = {"sessions", "archived_sessions", "skills", "agents", "scripts", "hooks",
                       "automations", "rules", "prompts", "memories", "state", "sqlite", "plugins",
                       "claude-cowork-transcript-imports", "dictation-history", "rollout-migrations"}
            if plan.get("include_managed_worktrees", False):
                folders.add("worktrees")
            excluded = []
            for child in base.iterdir():
                name = child.name.lower()
                if child.is_dir():
                    keep = name in folders
                else:
                    keep = (name.endswith((".json", ".jsonl", ".toml", ".yaml", ".yml", ".md", ".ini",
                                           ".sqlite", ".db", ".ps1", ".cmd", ".bat", ".py", ".sh"))
                            or name.startswith("config.toml.") or name == ".codex-global-state.json.bak"
                            or bool(re.fullmatch(r"(?:state|thread_history|memories|goals|queue)_\d+\.sqlite-(?:wal|shm|journal)", name)))
                    if name.startswith("logs_") or name in {"models_cache.json", "update-check.json"} or ".tmp-" in name:
                        keep = False
                if not keep:
                    excluded.append(child)
            excluded.extend(base / "plugins" / name for name in
                            ("cache", ".marketplace-plugin-source-staging", ".remote-plugin-install-staging", ".plugin-appserver"))
            return excluded
        if root.get("kind") == "skills":
            return [child for child in base.iterdir() if child.is_dir() and child.name != "skills"]
    if root.get("kind") == "codex" and not plan.get("include_managed_worktrees", False):
        return [Path(root["source"]) / "worktrees"]
    return []


def scope_exclusions(plan):
    return [{"root": root["label"], "relative_path": str(p.relative_to(root["source"])),
             "reason": "Outside Codex configuration, session history and user scripts/skills"}
            for root in plan["roots"] for p in excluded_subtrees(plan, root) if p.exists()]


def validate_plan(plan):
    if plan.get("schema") != 1 or not plan.get("roots"):
        raise ValueError("Plan needs schema 1 and at least one root")
    labels = set()
    paths = []
    for root in plan["roots"]:
        label = root["label"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", label) or label in labels:
            raise ValueError("Invalid or duplicate root label")
        labels.add(label)
        path = Path(root["source"])
        if not path.is_absolute() or not path.exists() or linklike(path):
            raise ValueError("Source root missing, non-absolute or linked")
        real = path.resolve()
        if any(under(real, p) or under(p, real) for p in paths):
            raise ValueError("Overlapping source roots")
        paths.append(real)


def walk(root, excluded=(), on_error=None):
    """Yield directories and links too; never follow junctions/symlinks."""
    pending = [Path(root)]
    while pending:
        p = pending.pop()
        if p in excluded:
            continue
        yield p
        if p.is_dir() and not linklike(p):
            try:
                with os.scandir(p) as entries:
                    children = sorted((Path(e.path) for e in entries), reverse=True)
            except OSError as exc:
                if on_error is None:
                    raise
                on_error(p, exc)
                continue
            pending.extend(children)


def sqlite_file(path):
    if not re.search(r"\.(sqlite\d*|db)$", path.name, re.I):
        return False
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\0"


def sqlite_snapshot(source, target):
    # sqlite3's transaction context manager does NOT close the connection.
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src, contextlib.closing(sqlite3.connect(target)) as dst:
        deadline = time.monotonic() + 60
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite backup exceeded 60 seconds")
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite snapshot failed integrity check")


def zip_timestamp(mtime):
    try:
        value = time.localtime(max(mtime, 0))[:6]
    except (OverflowError, OSError, ValueError):
        value = (1980, 1, 1, 0, 0, 0) if mtime <= 0 else (2107, 12, 31, 23, 59, 58)
    return min(max(value, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))


class SourceReadError(OSError):
    """A source read failed; destination write failures remain fatal."""


def discard_last_member(archive, name, offset):
    info = archive.NameToInfo.get(name)
    if info is None or not archive.filelist or archive.filelist[-1] is not info or info.header_offset != offset or archive._writing:
        raise RuntimeError("Cannot safely discard a failed ZIP member")
    archive.filelist.pop()
    del archive.NameToInfo[name]
    archive.start_dir = offset
    archive.fp.seek(offset)
    archive.fp.truncate(offset)


def copy_zip(z, path, name, mode, mtime, source_stream=None):
    sha = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo(name, zip_timestamp(mtime))
    info.compress_type = zipfile.ZIP_DEFLATED
    info._compresslevel = 1
    info.external_attr = (mode & 0o777) << 16
    source = path.open("rb") if source_stream is None else contextlib.nullcontext(source_stream)
    with source as src, z.open(info, "w", force_zip64=True) as dst:
        while True:
            try:
                data = src.read(1024 * 1024)
            except OSError as exc:
                raise SourceReadError(type(exc).__name__) from exc
            if not data:
                break
            sha.update(data)
            size += len(data)
            dst.write(data)
    return {"sha256": sha.hexdigest(), "size": size}


def create_backup(plan, output, allow_live=False, allow_partial=False):
    validate_plan(plan)
    output = check_destination(output)
    if output.suffix.lower() != ".zip":
        raise ValueError("Backups use standard .zip files; choose a .zip output filename")
    if os.path.lexists(output):
        raise ValueError("Backup output already exists")
    if any(os.path.lexists(path) for path in receipt_paths(output)):
        raise ValueError("A receipt for this output name already exists; choose a new archive name")
    output.parent.mkdir(parents=True, exist_ok=True)
    for root in plan["roots"]:
        if under(output.resolve(), Path(root["source"]).resolve()):
            raise ValueError("Backup output must be outside every source root")
    apps = running_apps()
    if apps and not allow_live:
        raise ValueError("Close ChatGPT and Codex, then run from a separate terminal; --allow-live produces a best-effort backup")
    issues = []
    if allow_live:
        issues.append({"type": "live-consistency", "detail": "Live override enabled: cross-file consistency is not guaranteed"})
    manifest = {"schema": 1, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "plan": plan, "runtime": runtime_inventory(), "entries": [], "issues": issues,
                "limitations": LIMITS, "running_apps_at_start": apps, "runtime_artifacts_not_copied": [],
                "intentional_exclusions": scope_exclusions(plan)}
    # Build and verify the ZIP on the destination disk before publication.
    with scratch(output.parent) as temp, scratch() as snapshots:
        part = temp / "backup.partial"
        with part.open("xb") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as z:
                for root in plan["roots"]:
                    base = Path(root["source"])
                    db_sidecars = set()
                    # DB files sort before their -shm/-wal sidecars in walk().
                    try:
                        def directory_error(path, exc):
                            relative = path.relative_to(base).as_posix()
                            issues.append({"type": "unreadable-directory", "name": root["label"] + ("/" + relative if relative != "." else ""), "error": type(exc).__name__})
                        for path in walk(base, excluded_subtrees(plan, root), directory_error if allow_live else None):
                            rel = path.relative_to(base).as_posix() if base.is_dir() else "__file__"
                            name = root["label"] + ("/" + rel if rel != "." else "")
                            safe_relative(name)
                            if path in db_sidecars:
                                continue
                            payload_started = False
                            member_offset = z.start_dir
                            try:
                                s = path.lstat()
                                entry = {"name": name, "mode": stat.S_IMODE(s.st_mode), "mtime_ns": s.st_mtime_ns}
                                if stat.S_ISSOCK(s.st_mode) or getattr(s, "st_reparse_tag", 0) == 0x80000023:
                                    manifest["runtime_artifacts_not_copied"].append({"name": name, "type": "socket"})
                                    continue
                                if linklike(path):
                                    entry.update(type="link", target=str(path.resolve()), link_kind="directory" if path.is_dir() else "file")
                                    if not any(under(path.resolve(), Path(r["source"]).resolve()) for r in plan["roots"]):
                                        issues.append({"type": "external-link", "name": name, "target": str(path.resolve())})
                                elif path.is_dir():
                                    entry["type"] = "directory"
                                    folder = zipfile.ZipInfo(name + "/", zip_timestamp(s.st_mtime))
                                    folder.external_attr = ((stat.S_IFDIR | entry["mode"]) << 16) | 0x10
                                    payload_started = True
                                    z.writestr(folder, b"")
                                elif path.is_file():
                                    entry["type"] = "file"
                                    read_path = path
                                    if sqlite_file(path):
                                        read_path = snapshots / "snapshot.sqlite"
                                        read_path.unlink(missing_ok=True)
                                        sqlite_snapshot(path, read_path)
                                        db_sidecars.update({Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")})
                                        entry["sqlite_snapshot"] = True
                                    # Open the source before creating a ZIP member, so locked
                                    # files can be skipped without a partial archive entry.
                                    with read_path.open("rb") as source:
                                        payload_started = True
                                        entry.update(copy_zip(z, read_path, name, entry["mode"], s.st_mtime, source_stream=source))
                                        if not entry.get("sqlite_snapshot"):
                                            after = os.fstat(source.fileno())
                                            if (s.st_size, s.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                                issues.append({"type": "changed-file", "name": name})
                                else:
                                    issues.append({"type": "special-file", "name": name})
                                    continue
                                manifest["entries"].append(entry)
                            except (OSError, ValueError, sqlite3.Error, TimeoutError) as e:
                                if allow_live and isinstance(e, SourceReadError):
                                    discard_last_member(z, name, member_offset)
                                    issues.append({"type": "unreadable-source", "name": name, "error": str(e)})
                                    continue
                                if allow_live and not payload_started:
                                    issues.append({"type": "unreadable-source", "name": name, "error": type(e).__name__})
                                    continue
                                # A partially written ZIP member cannot be silently accepted.
                                raise RuntimeError(f"Source could not be captured safely: {name} ({type(e).__name__})") from None
                    except OSError as e:
                        raise RuntimeError(f"Cannot enumerate source root {root['label']} ({type(e).__name__})") from None
                end_apps = running_apps()
                if end_apps and not allow_live:
                    raise ValueError("An app restarted during backup; output was not published")
                manifest["status"] = "best-effort" if issues else "complete-local-scope"
                if issues and not allow_partial and not allow_live:
                    raise ValueError("Coverage issues detected (for example external links); review the inventory and use --allow-partial only if acceptable")
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True))
                z.writestr("RESTORE-MAP.json", json.dumps({"manifest": manifest, "report": {"backup_status": manifest["status"]}}, ensure_ascii=True))
            os.fsync(raw.fileno())
        verified_manifest = verify_zip(part)
        # Exclusive creation refuses overwrite even if another process raced us.
        try:
            published_sha = hashlib.sha256()
            with part.open("rb") as src, output.open("xb") as dst:
                restrict_dir(output, is_directory=False)
                while data := src.read(1024 * 1024):
                    published_sha.update(data)
                    dst.write(data)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            # Never delete a pre-existing destination on FileExistsError.
            raise
        actual_sha = sha256_file(output)
        if actual_sha != published_sha.hexdigest():
            raise ValueError("Published archive failed the readback hash check; no successful receipt was written")
        receipts = write_receipts(output, build_receipt(output, verified_manifest, actual_sha))
    return {"status": manifest["status"], "output": str(output), "bytes": output.stat().st_size,
            "files": sum(e["type"] == "file" for e in manifest["entries"]), "issues": issues,
            "intentional_exclusions": manifest["intentional_exclusions"], "receipts": receipts}


def verify_zip(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = [x.filename for x in infos]
        names_set = set(names)
        if len(names) != len(set(n.casefold() for n in names)) or names.count("manifest.json") != 1:
            raise ValueError("Duplicate archive entries")
        if z.getinfo("manifest.json").file_size > 128 * 1024 * 1024:
            raise ValueError("Manifest is too large")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("schema") != 1:
            raise ValueError("Unsupported manifest")
        labels = [r["label"] for r in manifest["plan"]["roots"]]
        if len(labels) != len(set(labels)) or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", x) for x in labels):
            raise ValueError("Invalid manifest roots")
        expected = {"manifest.json"}
        if "RESTORE-MAP.json" in names_set:
            restore_info = z.getinfo("RESTORE-MAP.json")
            if restore_info.file_size > 128 * 1024 * 1024:
                raise ValueError("Restore map is too large")
            if json.loads(z.read("RESTORE-MAP.json")).get("manifest") != manifest:
                raise ValueError("Restore map disagrees with the capture manifest")
            expected.add("RESTORE-MAP.json")
        seen = set()
        types = {}
        for entry in manifest["entries"]:
            name = entry["name"]
            safe_relative(name)
            if name.split("/")[0] not in labels or name.casefold() in seen:
                raise ValueError("Invalid or duplicate manifest path")
            seen.add(name.casefold())
            types[name.casefold()] = entry["type"]
            if entry["type"] == "file":
                expected.add(name)
                info = z.getinfo(name)
                if info.file_size != entry["size"]:
                    raise ValueError("File size mismatch")
                sha = hashlib.sha256()
                with z.open(name) as f:
                    while data := f.read(1024 * 1024):
                        sha.update(data)
                if sha.hexdigest() != entry["sha256"]:
                    raise ValueError("File hash mismatch")
            elif entry["type"] == "directory":
                directory_name = name + "/"
                if directory_name in names_set:
                    if not z.getinfo(directory_name).is_dir():
                        raise ValueError("Invalid directory entry")
                    expected.add(directory_name)
            elif entry["type"] != "link":
                raise ValueError("Unsupported entry type")
        if names_set != expected:
            raise ValueError("Unlisted or missing payload files")
        for name in types:
            for parent in PurePosixPath(name).parents:
                if str(parent) in types and types[str(parent)] != "directory":
                    raise ValueError("File/link used as a parent directory")
        return manifest


def restore_zip(path, manifest, destination, apply=False):
    """Stage under root labels. Promotion into live profiles is deliberately separate."""
    target = check_destination(destination)
    if os.path.lexists(target):
        raise ValueError("Restore destination must not already exist; choose a fresh staging folder")
    report = {"mode": "apply" if apply else "preview", "destination": str(target),
              "backup_status": manifest["status"], "roots": manifest["plan"]["roots"],
              "files": sum(e["type"] == "file" for e in manifest["entries"]),
              "links_recorded_not_recreated": sum(e["type"] == "link" for e in manifest["entries"]),
              "issues": manifest["issues"], "intentional_exclusions": manifest.get("intentional_exclusions", []),
              "next": "Review RESTORE-MAP.json, then promote selected roots with restore.ps1; never merge SQLite databases."}
    if not apply:
        return report
    target.mkdir(parents=True)
    restrict_dir(target)
    with zipfile.ZipFile(path) as z:
        for entry in manifest["entries"]:
            out = target.joinpath(*safe_relative(entry["name"]).parts)
            check_destination(out)
            if entry["type"] == "directory":
                out.mkdir(parents=True, exist_ok=True)
            elif entry["type"] == "file":
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry["name"]) as src, out.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                if os.name != "nt":
                    out.chmod(entry["mode"] & 0o777)
                os.utime(out, ns=(entry["mtime_ns"], entry["mtime_ns"]))
    dump(target / "RESTORE-MAP.json", {"report": report, "manifest": manifest})
    return report


def scan_plan(plan):
    result = []
    for root in plan["roots"]:
        counts = {"label": root["label"], "source": root["source"], "files": 0, "bytes": 0, "links": [], "errors": []}
        try:
            for p in walk(Path(root["source"]), excluded_subtrees(plan, root)):
                try:
                    if linklike(p):
                        counts["links"].append({"path": str(p), "target": str(p.resolve())})
                    elif p.is_file():
                        counts["files"] += 1
                        counts["bytes"] += p.stat().st_size
                except OSError as e:
                    counts["errors"].append({"path": str(p), "error": type(e).__name__})
        except OSError as e:
            counts["errors"].append({"error": type(e).__name__})
        result.append(counts)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory", help="Discover app-data roots; no secret file contents")
    inv.add_argument("--profile", type=Path)
    inv.add_argument("--project", type=Path, action="append", default=[], help="Explicit opt-in: add a project folder")
    inv.add_argument("--include-managed-worktrees", action="store_true", help="Explicit opt-in: include Codex-managed project checkouts")
    inv.add_argument("--extra", type=Path, action="append", default=[])
    inv.add_argument("--chatgpt-export", type=Path, action="append", default=[])
    inv.add_argument("--plan-out", type=Path, required=True)
    inv.add_argument("--report-out", type=Path)
    inv.add_argument("--scan", action="store_true")
    make = commands.add_parser("backup")
    make.add_argument("--plan", type=Path, help="Optional saved plan; otherwise discover Codex config and sessions")
    destinations = make.add_mutually_exclusive_group()
    destinations.add_argument("--output", type=Path, help="Override with an exact archive file path")
    destinations.add_argument("--destination", type=Path, help="Backup folder; default on Windows: " + DEFAULT_DESTINATION)
    make.add_argument("--allow-live", action="store_true")
    make.add_argument("--allow-partial", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        plan = discover(args.profile)
        plan["include_managed_worktrees"] = args.include_managed_worktrees
        for kind, values in [("project", args.project), ("extra", args.extra), ("chatgpt-export", args.chatgpt_export)]:
            for path in values:
                add_root(plan, path, kind)
        validate_plan(plan)
        report = {"scope": plan["scope"], "roots": plan["roots"], "running_apps": running_apps(),
                  "intentional_exclusions": scope_exclusions(plan), "limitations": LIMITS}
        if args.scan:
            report["scan"] = scan_plan(plan)
        dump(args.plan_out, plan)
        if args.report_out:
            dump(args.report_out, report)
        print(json.dumps(report, indent=2))
    elif args.command == "backup":
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig")) if args.plan else discover()
        # Validate the app-only scope before copying data.
        validate_plan(plan)
        if running_apps() and not args.allow_live:
            raise ValueError("Close Codex/ChatGPT and run backup from an independent PowerShell window")
        output = choose_output(args.output, args.destination)
        result = create_backup(plan, output, args.allow_live, args.allow_partial)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "complete-local-scope" else 2
    else:
        manifest = verify_zip(args.archive)
        receipt_check = verify_receipts(args.archive, manifest)
        if args.command == "verify":
            print(json.dumps({"verified": True, "status": manifest["status"], "entries": len(manifest["entries"]), "issues": manifest["issues"],
                              "intentional_exclusions": manifest.get("intentional_exclusions", []), "receipts": receipt_check}, indent=2))
        else:
            print(json.dumps(restore_zip(args.archive, manifest, args.destination, args.apply), indent=2))
        return 0 if manifest["status"] == "complete-local-scope" else 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Cancelled; do not treat partial output as a backup", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Do not echo secrets or source file contents.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print("ERROR: " + message, file=sys.stderr)
        sys.exit(1)
