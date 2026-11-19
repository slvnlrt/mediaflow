"""Safety edges: reject dangerous storage targets and preserve existing ownership."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from mediaflow_lib.common import ProvisionError, write_private
from mediaflow_lib.config import DEFAULT_PORTS, validate


class ConfigSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.cfg = {
            "schema": 1, "project_name": "mediaflow-test", "uid": 1000, "gid": 1000,
            "data_path": str(self.base / "media"), "bind_ip": "127.0.0.1", "timezone": "UTC",
            "admin": {"username": "admin", "password": "synthetic-test-password", "email": "admin@example.invalid"},
            "vpn": {"mode": "none"}, "options": {"qui": False, "byparr": False, "wud": False},
            "ports": dict(DEFAULT_PORTS),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_system_or_project_ancestor_is_rejected_before_files_are_changed(self):
        for target in ["/", "/etc", "/home", str(self.base), str(self.root), str(self.root / ".state/apps")]:
            with self.subTest(target=target):
                self.cfg["data_path"] = target
                with self.assertRaises(ProvisionError):
                    validate(self.cfg, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlinked_storage_does_not_escape_to_other_directory(self):
        real = self.base / "existing"
        real.mkdir()
        marker = real / "keep"
        marker.write_text("unchanged")
        link = self.base / "media-link"
        link.symlink_to(real, target_is_directory=True)
        self.cfg["data_path"] = str(link / "child")
        with self.assertRaises(ProvisionError):
            validate(self.cfg, self.root)
        self.assertEqual(marker.read_text(), "unchanged")
        self.assertFalse((real / "child").exists())

    def test_private_write_does_not_follow_symlink(self):
        outside = self.base / "keep.json"
        outside.write_text("private original")
        link = self.root / "config.json"
        link.symlink_to(outside)
        with self.assertRaises(ProvisionError):
            write_private(link, "replacement")
        self.assertEqual(outside.read_text(), "private original")

    def test_replacement_does_not_retain_world_readable_permissions(self):
        path = self.root / "config.json"
        path.write_text("old")
        path.chmod(0o644)
        write_private(path, json.dumps({"private": True}))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"private": True})

    def test_root_application_identity_is_rejected(self):
        self.cfg["uid"] = 0
        with self.assertRaises(ProvisionError):
            validate(self.cfg, self.root)

    def test_qbit_cannot_take_an_internal_arr_port_even_if_host_ports_are_free(self):
        vpn = {"mode": "custom", "provider": "custom", "type": "openvpn",
               "openvpn_user": "fixture", "openvpn_password": "fixture"}
        for service in ("radarr", "sonarr"):
            with self.subTest(service=service):
                self.cfg["ports"] = {**DEFAULT_PORTS, service: DEFAULT_PORTS[service] + 10000,
                                     "qbittorrent": DEFAULT_PORTS[service]}
                self.cfg["vpn"] = {"mode": "none"}
                validate(self.cfg, self.root)  # Separate namespaces: this is legal.
                self.cfg["vpn"] = vpn
                with self.assertRaisesRegex(ProvisionError, "VPN namespace"):
                    validate(self.cfg, self.root)

    def test_public_wildcard_and_multicast_addresses_are_rejected(self):
        for address in ["0.0.0.0", "8.8.8.8", "224.0.0.1"]:
            with self.subTest(address=address):
                self.cfg["bind_ip"] = address
                with self.assertRaises(ProvisionError):
                    validate(self.cfg, self.root)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
