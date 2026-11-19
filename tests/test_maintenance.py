"""Archive containment, real SQLite roundtrip and failed-restore preservation."""
from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from mediaflow_lib.common import Context, ProvisionError
from mediaflow_lib.config import DEFAULT_PORTS
from mediaflow_lib.maintenance import _create_archive, _extract, inspect_backup, restore


class ArchiveSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        root = self.base / "installation"
        root.mkdir()
        self.cfg = {
            "schema": 1, "project_name": "archive-test", "uid": os.getuid() or 1000,
            "gid": os.getgid() or 1000, "data_path": str(self.base / "media"),
            "bind_ip": "127.0.0.1", "timezone": "UTC", "ports": dict(DEFAULT_PORTS),
            "admin": {"username": "admin", "password": "synthetic-test-password", "email": "admin@example.invalid"},
            "vpn": {"mode": "none"}, "options": {"qui": False, "byparr": False, "wud": False},
            "secrets": {}, "managed": {}, "phase": "ready",
        }
        self.ctx = Context(root, self.cfg)
        self.ctx.state.mkdir()
        self.db = self.ctx.state / "apps/radarr/radarr.db"
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("create table marker (value text)")
            conn.execute("insert into marker values ('archived')")
        (self.ctx.state / "compose.json").write_text("{}")
        self.dest = self.base / "backups"
        self.dest.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, entries=(), *, metadata=None, include_config=True):
        path = self.dest / "crafted.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            values = [("_backup_meta.json", metadata or {"schema": 1, "project_name": "archive-test"})]
            if include_config:
                values.append(("state/config.json", self.cfg))
            for name, value in values:
                body = json.dumps(value).encode()
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, BytesIO(body))
            for info, content in entries:
                info.size = len(content) if info.isfile() else 0
                tar.addfile(info, BytesIO(content) if info.isfile() else None)
        return path

    def test_roundtrip_preserves_database_and_confined_link_but_not_cache(self):
        cache = self.ctx.state / "apps/jellyfin-cache"
        cache.mkdir()
        (cache / "regenerable").write_text("cache")
        (self.db.parent / "current-db").symlink_to("radarr.db")
        archive = _create_archive(self.ctx, self.dest, {})
        stage = self.base / "restore"
        stage.mkdir()
        _extract(archive, stage, self.cfg, self.cfg)
        with closing(sqlite3.connect(stage / "apps/radarr/current-db")) as conn:
            self.assertEqual(conn.execute("select value from marker").fetchone()[0], "archived")
        self.assertFalse((stage / "apps/jellyfin-cache").exists())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(inspect_backup(archive)["config"]["project_name"], "archive-test")

    def test_traversal_absolute_and_foreign_paths_are_rejected(self):
        for name in ["state/../../outside", "/etc/passwd", "other/file", "state/../config.json"]:
            with self.subTest(name=name):
                archive = self.archive([(tarfile.TarInfo(name), b"malicious")])
                with self.assertRaises(ProvisionError):
                    inspect_backup(archive)
        self.assertFalse((self.base / "outside").exists())

    def test_devices_hardlinks_and_escaping_symlinks_are_rejected(self):
        for kind, target in [(tarfile.CHRTYPE, ""), (tarfile.LNKTYPE, "state/config.json"),
                             (tarfile.SYMTYPE, "/etc/passwd"), (tarfile.SYMTYPE, "../../outside")]:
            info = tarfile.TarInfo("state/entry")
            info.type, info.linkname = kind, target
            with self.subTest(kind=kind, target=target):
                with self.assertRaises(ProvisionError):
                    inspect_backup(self.archive([(info, b"")]))

    def test_writes_through_confined_symlink_are_rejected_before_extraction(self):
        link = tarfile.TarInfo("state/alias")
        link.type, link.linkname = tarfile.SYMTYPE, "."
        archive = self.archive([(link, b""), (tarfile.TarInfo("state/alias/file"), b"escape")])
        with self.assertRaises(ProvisionError):
            inspect_backup(archive)
        self.assertFalse((self.ctx.state / "file").exists())

    def test_duplicate_config_cannot_replace_validated_config(self):
        archive = self.archive([(tarfile.TarInfo("state/config.json"), b'{"schema":999}')])
        with self.assertRaises(ProvisionError):
            inspect_backup(archive)

    def test_missing_configuration_and_unknown_schema_are_rejected(self):
        with self.assertRaises(ProvisionError):
            inspect_backup(self.archive(include_config=False))
        with self.assertRaises(ProvisionError):
            inspect_backup(self.archive(metadata={"schema": 999, "project_name": "archive-test"}))

    def test_failed_post_swap_setup_recovers_current_database_and_configuration(self):
        archive = _create_archive(self.ctx, self.dest, {})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("update marker set value='before-restore'")
        events = []
        def compose(*args, **kwargs):
            events.append(args)
            return subprocess.CompletedProcess(args, 0, "radarr\n" if args[0] == "ps" else "", "")
        self.ctx.compose = compose
        with patch("mediaflow_lib.maintenance.ensure_network", side_effect=ProvisionError("injected")):
            with self.assertRaises(ProvisionError):
                restore(self.ctx, archive)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("select value from marker").fetchone()[0], "before-restore")
        self.assertEqual(self.ctx.cfg["project_name"], "archive-test")
        self.assertIn(("start", "radarr"), events)
        self.assertEqual(list(self.ctx.root.glob(".state-restore-*")), [])


if __name__ == "__main__":
    unittest.main()
