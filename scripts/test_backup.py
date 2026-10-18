"""Synthetic recovery and hostile-archive tests. Never touches the live profile."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import backup as b


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="codex-backup-test-")
        cls.base = Path(cls.temp.name)
        cls.home = cls.base / "old-profile"
        cls.root = cls.home / ".codex"
        (cls.root / "sessions" / "empty").mkdir(parents=True)
        (cls.root / "sessions" / "中文.jsonl").write_text('{"message":"synthetic history"}\n', encoding="utf-8")
        (cls.root / "auth.json").write_text('{"token":"FAKE-TEST-ONLY"}', encoding="utf-8")
        (cls.root / "sessions" / "large.bin").write_bytes(os.urandom(4 * 1024 * 1024 + 10000))
        (cls.root / "worktrees" / "checkout").mkdir(parents=True)
        (cls.root / "worktrees" / "checkout" / "project.py").write_text("# synthetic project code")
        (cls.home / "Projects" / "separate").mkdir(parents=True)
        (cls.home / "Projects" / "separate" / "project.py").write_text("# independent project")
        (cls.root / "hooks").mkdir()
        (cls.root / "hooks" / "hook.py").write_text("# synthetic app hook")
        (cls.root / "config.toml").write_text('model = "test-model"\n')
        cls.db = sqlite3.connect(cls.root / "state_5.sqlite")
        cls.db.execute("PRAGMA journal_mode=WAL")
        cls.db.execute("CREATE TABLE threads(id INTEGER PRIMARY KEY, title TEXT)")
        cls.db.execute("INSERT INTO threads(title) VALUES ('committed-in-WAL')")
        cls.db.commit()
        cls.plan = b.discover(cls.home)
        cls.archive = cls.base / "test.zip"
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={"test": True}):
            cls.result = b.create_backup(cls.plan, cls.archive)
        cls.plain = cls.base / "test.zip"
        cls.manifest = b.verify_zip(cls.plain)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.temp.cleanup()

    def test_full_roundtrip_and_wal(self):
        target = self.base / "restore-complete"
        report = b.restore_zip(self.plain, self.manifest, target, apply=True)
        self.assertEqual(report["backup_status"], "complete-local-scope")
        restored = target / "codex-home"
        self.assertEqual((restored / "auth.json").read_bytes(), (self.root / "auth.json").read_bytes())
        self.assertEqual((restored / "sessions" / "large.bin").read_bytes(), (self.root / "sessions" / "large.bin").read_bytes())
        self.assertTrue((restored / "sessions" / "empty").is_dir())
        self.assertTrue((restored / "sessions" / "中文.jsonl").is_file())
        self.assertFalse((restored / "state_5.sqlite-wal").exists())
        import contextlib
        with contextlib.closing(sqlite3.connect(restored / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT title FROM threads").fetchone(), ("committed-in-WAL",))

    def test_preview_writes_no_destination(self):
        target = self.base / "preview"
        self.assertEqual(b.restore_zip(self.plain, self.manifest, target)["mode"], "preview")
        self.assertFalse(target.exists())

    def test_default_destination_created_and_names_unique(self):
        folder = self.base / "new-default" / "backups"
        with mock.patch.object(b, "DEFAULT_DESTINATION", str(folder)):
            first = b.choose_output() if os.name == "nt" else b.choose_output(destination=folder)
            second = b.choose_output() if os.name == "nt" else b.choose_output(destination=folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(first.parent, folder)
        self.assertEqual(first.suffix, ".zip")
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())

    def test_receipts_list_every_entry_and_verify_without_secret_contents(self):
        json_path, text_path = b.receipt_paths(self.archive)
        receipt = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual({e["name"] for e in receipt["included"]}, {e["name"] for e in self.manifest["entries"]})
        self.assertEqual(receipt["archive_sha256"], b.sha256_file(self.archive))
        self.assertEqual(receipt["archive_bytes"], self.archive.stat().st_size)
        self.assertEqual(receipt["intentional_exclusions"], self.manifest["intentional_exclusions"])
        source = next(e for e in receipt["included"] if e["name"] == "codex-home/auth.json")
        self.assertEqual(source["source_path"], str(self.root / "auth.json"))
        self.assertNotIn("FAKE-TEST-ONLY", json_path.read_text(encoding="utf-8"))
        self.assertNotIn("FAKE-TEST-ONLY", text_path.read_text(encoding="utf-8"))
        self.assertIn('codex-home/sessions/empty', text_path.read_text(encoding="utf-8"))
        self.assertEqual(b.verify_receipts(self.archive, self.manifest), {"json": "verified", "text": "verified"})

    def test_changed_receipt_is_rejected(self):
        import shutil
        for kind, suffix in [("json", ".receipt.json"), ("text", ".receipt.txt")]:
            with self.subTest(kind=kind):
                folder = self.base / ("changed-receipt-" + kind)
                folder.mkdir()
                archive = folder / self.archive.name
                shutil.copyfile(self.archive, archive)
                for original, target in zip(b.receipt_paths(self.archive), b.receipt_paths(archive)):
                    shutil.copyfile(original, target)
                path = archive.with_suffix(suffix)
                if kind == "json":
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                    receipt["counts"]["files"] += 1
                    path.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    with path.open("a", encoding="utf-8") as f:
                        f.write("tampered")
                with self.assertRaises(ValueError):
                    b.verify_receipts(archive, self.manifest)

    def test_receipt_name_collision_refuses_backup(self):
        out = self.base / "receipt-collision.zip"
        collision = b.receipt_paths(out)[0]
        collision.write_text("preserve existing", encoding="utf-8")
        with self.assertRaises(ValueError):
            b.create_backup(self.plan, out)
        self.assertFalse(out.exists())
        self.assertEqual(collision.read_text(encoding="utf-8"), "preserve existing")

    def test_app_only_default_preserves_history_and_scripts_without_projects(self):
        names = {entry["name"] for entry in self.manifest["entries"]}
        self.assertIn("codex-home/config.toml", names)
        self.assertIn("codex-home/hooks/hook.py", names)
        self.assertIn("codex-home/sessions/中文.jsonl", names)
        self.assertIn("codex-home/state_5.sqlite", names)
        self.assertFalse(any("worktrees" in name for name in names))
        self.assertFalse(any(Path(root["source"]) == self.home / "Projects" / "separate" for root in self.plan["roots"]))
        self.assertEqual(self.result["status"], "complete-local-scope")
        self.assertIn("worktrees", {e["relative_path"] for e in self.manifest["intentional_exclusions"]})

    def test_managed_worktree_backup_requires_explicit_opt_in(self):
        plan = json.loads(json.dumps(self.plan))
        plan["include_managed_worktrees"] = True
        archive = self.base / "with-worktrees.zip"
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}):
            b.create_backup(plan, archive)
        plain = self.base / "with-worktrees.zip"
        manifest = b.verify_zip(plain)
        self.assertIn("codex-home/worktrees/checkout/project.py", {e["name"] for e in manifest["entries"]})
        self.assertEqual(manifest["intentional_exclusions"], [])

    def test_narrow_discovery_ignores_app_installs_and_external_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for name in (".codex", ".agents", "AppData/Local/OpenAI", ".vscode/extensions/openai.chatgpt-test", "Projects"):
                (home / name).mkdir(parents=True)
            plan = b.discover(home)
            self.assertEqual(plan["scope"], "codex-config-sessions")
            self.assertEqual({r["label"] for r in plan["roots"]}, {"codex-home", "shared-agents"})

    def test_narrow_scope_keeps_history_and_scripts_but_excludes_project_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            root = home / ".codex"
            keep = ["config.toml", "auth.json", "state_9.sqlite", "thread_history_2.sqlite",
                    "sessions/chat.jsonl", "archived_sessions/chat.jsonl", "scripts/helper.py",
                    "skills/example/SKILL.md", "plugins/data/settings.json"]
            omit = ["worktrees/project/main.py", ".chatgpt-projects/demo/main.py",
                    "visualizations/demo/index.html", "plugins/cache/demo/server.js",
                    "packages/runtime.exe", "logs_2.sqlite", "models_cache.json", ".codex-provisioning-x.guard"]
            for name in keep + omit:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic")
            plan = b.discover(home)
            paths = {p.relative_to(root).as_posix() for p in b.walk(root, b.excluded_subtrees(plan, plan["roots"][0]))}
            self.assertTrue(set(keep) <= paths)
            self.assertFalse(set(omit) & paths)

    def test_existing_restore_refused(self):
        target = self.base / "existing"
        target.mkdir(exist_ok=True)
        (target / "keep").write_text("keep")
        with self.assertRaises(ValueError):
            b.restore_zip(self.plain, self.manifest, target, apply=True)
        self.assertEqual((target / "keep").read_text(), "keep")

    def test_truncated_zip_is_rejected(self):
        path = self.base / "truncated.zip"
        path.write_bytes(self.archive.read_bytes()[:-20])
        with self.assertRaises(zipfile.BadZipFile):
            b.verify_zip(path)

    def test_output_overwrite_refused(self):
        before = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            b.create_backup(self.plan, self.archive)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_output_inside_source_refused(self):
        with self.assertRaises(ValueError):
            b.create_backup(self.plan, self.root / "bad.zip")

    def test_running_app_refused(self):
        with mock.patch.object(b, "running_apps", return_value=["Codex"]):
            with self.assertRaises(ValueError):
                b.create_backup(self.plan, self.base / "blocked.zip")
        self.assertFalse((self.base / "blocked.zip").exists())

    def test_live_override_is_not_complete(self):
        with mock.patch.object(b, "running_apps", return_value=["Codex"]), mock.patch.object(b, "runtime_inventory", return_value={}):
            result = b.create_backup(self.plan, self.base / "live.zip", allow_live=True)
        self.assertEqual(result["status"], "best-effort")

    def test_live_locked_file_is_skipped_and_receipted(self):
        original = Path.open
        def open_readable(source, *args, **kwargs):
            if source == self.root / "auth.json":
                raise PermissionError("synthetic exclusive file lock")
            return original(source, *args, **kwargs)
        output = self.base / "live-locked.zip"
        with mock.patch.object(b, "running_apps", return_value=["Codex"]), mock.patch.object(b, "runtime_inventory", return_value={}), mock.patch.object(Path, "open", new=open_readable):
            result = b.create_backup(self.plan, output, allow_live=True)
        self.assertEqual(result["status"], "best-effort")
        manifest = b.verify_zip(output)
        names = {entry["name"] for entry in manifest["entries"]}
        self.assertNotIn("codex-home/auth.json", names)
        self.assertIn("codex-home/config.toml", names)
        self.assertIn({"type": "unreadable-source", "name": "codex-home/auth.json", "error": "PermissionError"}, manifest["issues"])
        self.assertEqual(b.verify_receipts(output, manifest), {"json": "verified", "text": "verified"})

    def test_old_and_future_timestamps_are_safe_for_zip(self):
        import io
        source = self.base / "timestamp-source"
        source.write_bytes(b"test")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            b.copy_zip(archive, source, "old.txt", 0o600, 0)
            b.copy_zip(archive, source, "future.txt", 0o600, 10**15)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            self.assertEqual(archive.getinfo("old.txt").date_time, (1980, 1, 1, 0, 0, 0))
            self.assertLessEqual(archive.getinfo("future.txt").date_time[0], 2107)
            self.assertEqual(archive.read("old.txt"), b"test")

    def test_live_midstream_read_failure_discards_partial_entry_and_continues(self):
        original = Path.open
        class FailingReader:
            def __init__(self, stream):
                self.stream = stream
                self.reads = 0
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(self, count):
                self.reads += 1
                if self.reads > 1:
                    raise PermissionError("synthetic byte-range lock")
                return self.stream.read(8)
        def open_source(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return FailingReader(stream) if path == self.root / "auth.json" else stream
        output = self.base / "live-midstream.zip"
        with mock.patch.object(b, "running_apps", return_value=["Codex"]), mock.patch.object(b, "runtime_inventory", return_value={}), mock.patch.object(Path, "open", new=open_source):
            result = b.create_backup(self.plan, output, allow_live=True)
        manifest = b.verify_zip(output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("codex-home/auth.json", archive.namelist())
            self.assertEqual(archive.read("codex-home/config.toml"), (self.root / "config.toml").read_bytes())
            self.assertIsNone(archive.testzip())
        self.assertEqual(result["status"], "best-effort")
        self.assertIn({"type": "unreadable-source", "name": "codex-home/auth.json", "error": "PermissionError"}, manifest["issues"])
        self.assertEqual(b.verify_receipts(output, manifest), {"json": "verified", "text": "verified"})

    def test_live_unreadable_directory_does_not_stop_other_data(self):
        original = os.scandir
        def readable_directory(path):
            if Path(path) == self.root / "sessions":
                raise PermissionError("synthetic directory lock")
            return original(path)
        output = self.base / "live-directory.zip"
        with mock.patch.object(b, "running_apps", return_value=["Codex"]), mock.patch.object(b, "runtime_inventory", return_value={}), mock.patch.object(b.os, "scandir", side_effect=readable_directory):
            result = b.create_backup(self.plan, output, allow_live=True)
        self.assertEqual(result["status"], "best-effort")
        manifest = b.verify_zip(output)
        self.assertIn("codex-home/config.toml", {entry["name"] for entry in manifest["entries"]})
        self.assertIn({"type": "unreadable-directory", "name": "codex-home/sessions", "error": "PermissionError"}, manifest["issues"])

    def test_overlap_refused(self):
        with self.assertRaises(ValueError):
            b.add_root(json.loads(json.dumps(self.plan)), self.root / "sessions", "project")

    def test_unsafe_names(self):
        for name in ("../escape", "/absolute", "root/../escape", "root/C:bad", "root\\bad", "root/CON.txt", "root/trailing.", "root//bad", "root/./bad"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                b.safe_relative(name)

    def test_unlisted_payload_refused(self):
        path = self.base / "extra.zip"
        with zipfile.ZipFile(self.plain) as src, zipfile.ZipFile(path, "w") as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            dst.writestr("codex-home/unlisted.txt", "bad")
        with self.assertRaises(ValueError):
            b.verify_zip(path)

    def test_hash_mismatch_refused(self):
        path = self.base / "hash.zip"
        with zipfile.ZipFile(self.plain) as src, zipfile.ZipFile(path, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith("auth.json"):
                    data = bytes([data[0] ^ 1]) + data[1:]
                dst.writestr(item, data)
        with self.assertRaises(ValueError):
            b.verify_zip(path)

    def test_standard_zip_is_readable_without_password(self):
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.read("codex-home/auth.json"), (self.root / "auth.json").read_bytes())
            self.assertTrue(all(not (info.flag_bits & 1) for info in archive.infolist()))
            self.assertIsNone(archive.testzip())
            self.assertIn("codex-home/sessions/empty/", archive.namelist())
            self.assertEqual(json.loads(archive.read("RESTORE-MAP.json"))["manifest"], self.manifest)

    def test_windows_zip_extraction_preserves_empty_folders_and_restore_map(self):
        if os.name != "nt":
            self.skipTest("Windows ZIP extraction")
        destination = self.base / "windows-extracted"
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = "Add-Type -AssemblyName System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory(" + quote(self.archive) + ", " + quote(destination) + ")"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((destination / "codex-home" / "auth.json").read_bytes(), (self.root / "auth.json").read_bytes())
        self.assertTrue((destination / "codex-home" / "sessions" / "empty").is_dir())
        self.assertEqual(json.loads((destination / "RESTORE-MAP.json").read_text())["manifest"], self.manifest)

    def test_cli_backup_verify_restore_with_standard_library_only(self):
        import sys
        plan_path = self.base / "cli-plan.json"
        b.dump(plan_path, self.plan)
        destination = self.base / "cli-backups"
        script = Path(__file__).with_name("backup.py")
        command = [sys.executable, "-S", str(script)]
        # This override applies only to the synthetic fixture; it does not copy live app data.
        made = subprocess.run(command + ["backup", "--plan", str(plan_path), "--destination", str(destination), "--allow-live"], capture_output=True, text=True)
        self.assertEqual(made.returncode, 2, made.stderr)
        output = Path(json.loads(made.stdout)["output"])
        self.assertEqual(output.suffix, ".zip")
        self.assertTrue(zipfile.is_zipfile(output))
        checked = subprocess.run(command + ["verify", str(output)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["verified"])
        restored = self.base / "cli-restored"
        recovered = subprocess.run(command + ["restore", str(output), "--destination", str(restored), "--apply"], capture_output=True, text=True)
        self.assertEqual(recovered.returncode, 2, recovered.stderr)
        self.assertEqual((restored / "codex-home" / "config.toml").read_bytes(), (self.root / "config.toml").read_bytes())

    def test_file_root_and_project_inclusion(self):
        selected = self.base / "project"
        selected.mkdir()
        (selected / ".env").write_text("FAKE=YES")
        (selected / ".git").mkdir()
        (selected / "untracked.txt").write_text("uncommitted")
        export = self.base / "export.zip"
        export.write_bytes(b"synthetic export")
        plan = json.loads(json.dumps(self.plan))
        b.add_root(plan, selected, "project")
        b.add_root(plan, export, "chatgpt-export")
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}):
            b.create_backup(plan, self.base / "project.zip")
        plain = self.base / "project.zip"
        manifest = b.verify_zip(plain)
        names = {e["name"] for e in manifest["entries"]}
        self.assertIn("project-1/.env", names)
        self.assertIn("project-1/.git", names)
        self.assertIn("project-1/untracked.txt", names)
        self.assertIn("chatgpt-export-2/__file__", names)
        receipt = json.loads(b.receipt_paths(self.base / "project.zip")[0].read_text(encoding="utf-8"))
        self.assertEqual(next(e["source_path"] for e in receipt["included"] if e["name"] == "chatgpt-export-2/__file__"), str(export))

    def test_missing_source_is_failure(self):
        plan = json.loads(json.dumps(self.plan))
        plan["roots"][0]["source"] = str(self.base / "not-there")
        with self.assertRaises(ValueError):
            b.create_backup(plan, self.base / "missing.zip")

    def test_read_error_never_publishes(self):
        out = self.base / "read-error.zip"
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}), mock.patch.object(b, "copy_zip", side_effect=PermissionError("test")):
            with self.assertRaises(RuntimeError):
                b.create_backup(self.plan, out, allow_partial=True)
        self.assertFalse(out.exists())

    def test_invalid_sqlite_never_publishes(self):
        out = self.base / "sqlite-error.zip"
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}), mock.patch.object(b, "sqlite_snapshot", side_effect=sqlite3.DatabaseError("test")):
            with self.assertRaises(RuntimeError):
                b.create_backup(self.plan, out)
        self.assertFalse(out.exists())

    def test_powershell_promotion_preview_apply_and_existing(self):
        if os.name != "nt":
            self.skipTest("PowerShell Windows promoter")
        stage = self.base / "ps-stage"
        b.restore_zip(self.plain, self.manifest, stage, apply=True)
        target = self.base / "ps-target"
        script = Path(__file__).with_name("restore.ps1")
        def run(apply=False):
            quote = lambda p: "'" + str(p).replace("'", "''") + "'"
            # Only the synthetic child-process fixture shadows the process check.
            command = "function Get-Process { @() }; & " + quote(script) + " -StagingDirectory " + quote(stage) + " -Label codex-home -TargetPath " + quote(target)
            if apply:
                command += " -Apply"
            return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
        preview = run()
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(target.exists())
        applied = run(True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((target / "auth.json").read_bytes(), (self.root / "auth.json").read_bytes())
        self.assertNotEqual(run(True).returncode, 0)

    def test_powershell_single_file_root(self):
        if os.name != "nt":
            self.skipTest("PowerShell Windows promoter")
        import hashlib
        import time
        stage = self.base / "single-stage"
        (stage / "export").mkdir(parents=True)
        data = b"synthetic single-file export"
        (stage / "export" / "__file__").write_bytes(data)
        manifest = {"plan": {"roots": [{"label": "export", "is_directory": False}]}, "entries": [{"name": "export/__file__", "type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}
        b.dump(stage / "RESTORE-MAP.json", {"manifest": manifest})
        script = Path(__file__).with_name("restore.ps1")
        target = self.base / "single-target.zip"
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = "function Get-Process { @() }; & " + quote(script) + " -StagingDirectory " + quote(stage) + " -Label export -TargetPath " + quote(target) + " -Apply"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), data)

    def test_powershell_internal_directory_link(self):
        if os.name != "nt":
            self.skipTest("Windows directory link restoration")
        import hashlib
        stage = self.base / "link-stage"
        (stage / "linked-root" / "real").mkdir(parents=True)
        data = b"linked data"
        (stage / "linked-root" / "real" / "data").write_bytes(data)
        old = self.base / "original-link-root"
        shared_old = self.base / "old-shared"
        shared_new = self.base / "restored-shared"
        shared_new.mkdir()
        (shared_new / "shared-data").write_bytes(data)
        manifest = {"plan": {"roots": [{"label": "linked-root", "source": str(old), "is_directory": True}, {"label": "shared", "source": str(shared_old), "is_directory": True}]}, "entries": [
            {"name": "linked-root/real/data", "type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            {"name": "linked-root/current", "type": "link", "target": str(old / "real"), "link_kind": "directory"},
            {"name": "linked-root/shared", "type": "link", "target": str(shared_old), "link_kind": "directory"}]}
        b.dump(stage / "RESTORE-MAP.json", {"manifest": manifest})
        target = self.base / "link-promoted"
        script = Path(__file__).with_name("restore.ps1")
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = "function Get-Process { @() }; & " + quote(script) + " -StagingDirectory " + quote(stage) + " -Label linked-root -TargetPath " + quote(target) + " -AdditionalRootTargets @{ 'shared' = " + quote(shared_new) + " } -Apply"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(b.linklike(target / "current"))
        self.assertEqual((target / "current" / "data").read_bytes(), data)
        self.assertEqual((target / "shared" / "shared-data").read_bytes(), data)

    def test_runtime_socket_is_recorded_not_copied(self):
        from types import SimpleNamespace
        import stat
        source = self.base / "socket-source"
        source.mkdir()
        path = source / "app.sock"
        path.write_bytes(b"synthetic socket metadata fixture")
        original = Path.lstat
        def socket_lstat(p, *args, **kwargs):
            result = original(p, *args, **kwargs)
            if p == path:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_mtime_ns=result.st_mtime_ns,
                                       st_file_attributes=0x400, st_reparse_tag=0x80000023)
            return result
        with mock.patch.object(Path, "lstat", new=socket_lstat):
            plan = {"schema": 1, "profile": str(self.base), "roots": [{"label": "socket", "source": str(source), "is_directory": True}]}
            archive = self.base / "socket.zip"
            with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}):
                result = b.create_backup(plan, archive)
            self.assertEqual(result["status"], "complete-local-scope")
            plain = self.base / "socket.zip"
            manifest = b.verify_zip(plain)
            self.assertEqual(manifest["runtime_artifacts_not_copied"], [{"name": "socket/app.sock", "type": "socket"}])

    def test_external_junction_coverage_and_restore_ancestor(self):
        if os.name != "nt":
            self.skipTest("Windows junction fixture")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "external.txt").write_text("not silently captured")
        linked_root = self.base / "link-source"
        linked_root.mkdir()
        junction = linked_root / "link"
        ps = "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''") + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null"
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, check=True)
        plan = {"schema": 1, "profile": str(self.base), "roots": [{"label": "linked", "source": str(linked_root), "kind": "extra", "home_relative": None, "is_directory": True}]}
        with mock.patch.object(b, "running_apps", return_value=[]), mock.patch.object(b, "runtime_inventory", return_value={}):
            with self.assertRaises(ValueError):
                b.create_backup(plan, self.base / "links-blocked.zip")
            result = b.create_backup(plan, self.base / "links-partial.zip", allow_partial=True)
        self.assertEqual(result["status"], "best-effort")
        self.assertEqual(result["issues"][0]["type"], "external-link")
        with self.assertRaises(ValueError):
            b.restore_zip(self.plain, self.manifest, junction / "escape", apply=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
