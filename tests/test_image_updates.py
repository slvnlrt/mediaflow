"""Tests for image update resolution, planning, persistence and failure edges."""
from __future__ import annotations

import json
import copy
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from mediaflow_lib.common import ProvisionError
from mediaflow_lib.image_updates import (
    UpdatePlan, active_services, check_buildx, display_plan, ensure_pins,
    load_tracking, merge_overrides, parse_pinned, plan_updates, resolve_digest,
)


class TestParsePinned(unittest.TestCase):
    def test_with_digest(self):
        name, digest = parse_pinned("lscr.io/linuxserver/radarr:latest@sha256:abc123")
        self.assertEqual(name, "lscr.io/linuxserver/radarr:latest")
        self.assertEqual(digest, "sha256:abc123")

    def test_without_digest(self):
        name, digest = parse_pinned("lscr.io/linuxserver/radarr:latest")
        self.assertEqual(name, "lscr.io/linuxserver/radarr:latest")
        self.assertIsNone(digest)

    def test_at_in_digest_only(self):
        name, digest = parse_pinned("ghcr.io/org/repo:v1@sha256:deadbeef")
        self.assertEqual(name, "ghcr.io/org/repo:v1")
        self.assertEqual(digest, "sha256:deadbeef")


class TestLoadTracking(unittest.TestCase):
    def test_explicit_tracking_overrides_default(self):
        base, tracking = load_tracking()
        self.assertEqual(tracking["wud"], "getwud/wud:latest")

    def test_default_tracking_uses_tag(self):
        base, tracking = load_tracking()
        self.assertEqual(tracking["radarr"], "lscr.io/linuxserver/radarr:latest")
        self.assertEqual(tracking["recyclarr"], "ghcr.io/recyclarr/recyclarr:8")
        self.assertEqual(tracking["port-sync"], "python:3.13-alpine")

    def test_all_images_have_tracking(self):
        base, tracking = load_tracking()
        for service in base:
            self.assertIn(service, tracking)


class TestActiveServices(unittest.TestCase):
    def _cfg(self, **overrides):
        cfg = {"vpn": {"mode": "none"},
               "options": {"qui": False, "byparr": False, "wud": False}}
        cfg.update(overrides)
        return cfg

    def test_base_services(self):
        services = active_services(self._cfg())
        for name in ("qbittorrent", "radarr", "sonarr", "prowlarr", "jellyfin", "seerr", "recyclarr"):
            self.assertIn(name, services)
        self.assertNotIn("gluetun", services)
        self.assertNotIn("socket-proxy", services)

    def test_vpn_adds_gluetun(self):
        services = active_services(self._cfg(vpn={"mode": "wireguard"}))
        self.assertIn("gluetun", services)
        self.assertNotIn("port-sync", services)

    def test_vpn_port_forwarding_adds_port_sync(self):
        services = active_services(self._cfg(vpn={"mode": "wireguard", "port_forwarding": True}))
        self.assertIn("gluetun", services)
        self.assertIn("port-sync", services)

    def test_wud_adds_socket_proxy(self):
        cfg = self._cfg(options={"qui": False, "byparr": False, "wud": True})
        services = active_services(cfg)
        self.assertIn("wud", services)
        self.assertIn("socket-proxy", services)


class TestCheckBuildx(unittest.TestCase):
    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_missing_buildx_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        with self.assertRaises(ProvisionError) as cm:
            check_buildx()
        self.assertIn("buildx", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_docker_not_found_raises(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        with self.assertRaises(ProvisionError) as cm:
            check_buildx()
        self.assertIn("not installed", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)
        with self.assertRaises(ProvisionError) as cm:
            check_buildx()
        self.assertIn("timed out", str(cm.exception))


class TestResolveDigest(unittest.TestCase):
    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"digest": "sha256:" + "a" * 64, "mediaType": "application/vnd.oci.image.index.v1+json"}))
        digest = resolve_digest("repo:latest")
        self.assertEqual(digest, "sha256:" + "a" * 64)

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_registry_error_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        with self.assertRaises(ProvisionError) as cm:
            resolve_digest("repo:latest")
        self.assertIn("Cannot resolve", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_malformed_json_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json at all")
        with self.assertRaises(ProvisionError) as cm:
            resolve_digest("repo:latest")
        self.assertIn("Unexpected", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_missing_digest_field_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"mediaType":"something","size":0}')
        with self.assertRaises(ProvisionError) as cm:
            resolve_digest("repo:latest")
        self.assertIn("No valid", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=120)
        with self.assertRaises(ProvisionError) as cm:
            resolve_digest("repo:latest")
        self.assertIn("timed out", str(cm.exception))

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_invalid_descriptor_digests_are_rejected(self, mock_run):
        for descriptor in [[], None, {"digest": None}, {"digest": "sha256:abc"}, {"digest": "sha256:" + "g" * 64}]:
            with self.subTest(descriptor=descriptor):
                mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(descriptor))
                with self.assertRaises(ProvisionError):
                    resolve_digest("repo:latest")

    @patch("mediaflow_lib.image_updates.subprocess.run")
    def test_registry_error_does_not_print_raw_credentials(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="private-registry-diagnostic")
        with self.assertRaises(ProvisionError) as error:
            resolve_digest("repo:latest")
        self.assertNotIn("private-registry-diagnostic", str(error.exception))


class TestTransactionalVerification(unittest.TestCase):
    def test_api_failure_rolls_back_and_verifies_old_pins_including_inactive(self):
        from mediaflow_lib import maintenance
        ctx = MagicMock()
        ctx.root = Path("/fixture")
        ctx.cfg = {
            "phase": "ready", "managed": {"published_services": ["radarr"]},
            "image_overrides": {"radarr": "r@sha256:old", "wud": "w@sha256:disabled"},
        }
        plan = UpdatePlan("radarr", "r:latest", "r@sha256:old", "sha256:old",
                          "sha256:new", "r@sha256:new", True)
        seen = []

        def verify(current):
            seen.append(copy.deepcopy(current.cfg["image_overrides"]))
            if current.cfg["image_overrides"]["radarr"] == "r@sha256:new":
                raise ProvisionError("New API unavailable")

        with patch.object(maintenance, "_running_services", return_value=["radarr"]), \
             patch.object(maintenance, "snapshot_images", return_value=({}, {"radarr": "r@sha256:old"})), \
             patch.object(maintenance, "_destination", return_value=Path("/backups")), \
             patch.object(maintenance, "_stop"), \
             patch.object(maintenance, "_create_archive", return_value=Path("/backups/state.tar.gz")), \
             patch.object(maintenance, "write_compose"), \
             patch.object(maintenance, "restore"), \
             patch.object(maintenance.subprocess, "run", return_value=MagicMock(returncode=0)):
            with self.assertRaises(ProvisionError):
                maintenance.update(ctx, [plan], verify=verify)
        self.assertEqual([pins["radarr"] for pins in seen], ["r@sha256:new", "r@sha256:old"])
        self.assertEqual(ctx.cfg["image_overrides"],
                         {"radarr": "r@sha256:old", "wud": "w@sha256:disabled"})
        self.assertEqual(ctx.cfg["phase"], "ready")

    def test_failed_pin_inspection_never_falls_back_to_starter_images(self):
        from mediaflow_lib.cli import _adopt_pins
        ctx = MagicMock()
        ctx.cfg = {"vpn": {"mode": "none"}, "options": {}}
        with patch("mediaflow_lib.maintenance.snapshot_images", side_effect=ProvisionError("inspection failed")):
            with self.assertRaises(ProvisionError):
                _adopt_pins(ctx)
        self.assertNotIn("image_overrides", ctx.cfg)


class TestPlanUpdates(unittest.TestCase):
    def _cfg(self, overrides=None):
        return {"vpn": {"mode": "none"},
                "options": {"qui": False, "byparr": False, "wud": False},
                "image_overrides": dict(overrides or {})}

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_all_current_reports_no_changes(self, _buildx, mock_resolve):
        base, _ = load_tracking()
        _, base_digest = parse_pinned(base["radarr"])
        mock_resolve.return_value = base_digest
        plans = plan_updates(self._cfg(), ["radarr"])
        self.assertEqual(len(plans), 1)
        self.assertFalse(plans[0].changed)

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_changed_digest_detected(self, _buildx, mock_resolve):
        mock_resolve.return_value = "sha256:aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666777788889999aaaa"
        plans = plan_updates(self._cfg(), ["radarr"])
        self.assertTrue(plans[0].changed)
        self.assertIn("sha256:aaaa1111", plans[0].new_ref)

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_partial_selection_limits_scope(self, _buildx, mock_resolve):
        mock_resolve.return_value = "sha256:ffff0000"
        plans = plan_updates(self._cfg(), ["radarr", "sonarr"])
        self.assertEqual({p.service for p in plans}, {"radarr", "sonarr"})

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_resolution_failure_raises_all_errors(self, _buildx, mock_resolve):
        mock_resolve.side_effect = ProvisionError("network error")
        with self.assertRaises(ProvisionError) as cm:
            plan_updates(self._cfg(), ["radarr", "sonarr"])
        self.assertIn("resolution failed", str(cm.exception).lower())

    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_missing_service_raises(self, _buildx):
        with self.assertRaises(ProvisionError) as cm:
            plan_updates(self._cfg(), ["nonexistent_service"])
        self.assertIn("No image defined", str(cm.exception))

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_uses_override_digest_for_comparison(self, _buildx, mock_resolve):
        cfg = self._cfg(overrides={"radarr": "lscr.io/linuxserver/radarr:latest@sha256:overridden"})
        mock_resolve.return_value = "sha256:overridden"
        plans = plan_updates(cfg, ["radarr"])
        self.assertFalse(plans[0].changed)

    @patch("mediaflow_lib.image_updates.resolve_digest")
    @patch("mediaflow_lib.image_updates.check_buildx")
    def test_never_removes_overrides_during_resolution(self, _buildx, mock_resolve):
        cfg = self._cfg(overrides={"radarr": "lscr.io/linuxserver/radarr:latest@sha256:pinned"})
        mock_resolve.return_value = "sha256:pinned"
        plan_updates(cfg, ["radarr"])
        self.assertIn("radarr", cfg["image_overrides"])


class TestMergeOverrides(unittest.TestCase):
    def test_preserves_inactive_overrides(self):
        current = {"jellyfin": "jf:latest@sha256:old", "wud": "wud:9@sha256:old"}
        plans = [UpdatePlan(service="jellyfin", tracking_ref="jf:latest",
                            current_ref="jf:latest@sha256:old", current_digest="sha256:old",
                            new_digest="sha256:new", new_ref="jf:latest@sha256:new", changed=True)]
        result = merge_overrides(current, plans)
        self.assertEqual(result["jellyfin"], "jf:latest@sha256:new")
        self.assertEqual(result["wud"], "wud:9@sha256:old")

    def test_unchanged_plans_not_merged(self):
        current = {}
        plans = [UpdatePlan(service="radarr", tracking_ref="r:latest",
                            current_ref="r:latest@sha256:same", current_digest="sha256:same",
                            new_digest="sha256:same", new_ref="r:latest@sha256:same", changed=False)]
        result = merge_overrides(current, plans)
        self.assertEqual(result, {})


class TestEnsurePins(unittest.TestCase):
    def test_adopts_container_refs_for_unpinned(self):
        cfg: dict = {}
        container_refs = {"radarr": "lscr.io/linuxserver/radarr:latest@sha256:containerdigest"}
        result = ensure_pins(cfg, ["radarr"], container_refs)
        self.assertTrue(result)
        self.assertEqual(cfg["image_overrides"]["radarr"],
                         "lscr.io/linuxserver/radarr:latest@sha256:containerdigest")

    def test_falls_back_to_base_when_no_container(self):
        cfg: dict = {}
        result = ensure_pins(cfg, ["radarr"], {})
        self.assertTrue(result)
        base, _ = load_tracking()
        self.assertEqual(cfg["image_overrides"]["radarr"], base["radarr"])

    def test_preserves_existing_overrides(self):
        cfg = {"image_overrides": {"radarr": "custom:latest@sha256:existing"}}
        result = ensure_pins(cfg, ["radarr"],
                             {"radarr": "lscr.io/linuxserver/radarr:latest@sha256:containerdigest"})
        self.assertFalse(result)
        self.assertEqual(cfg["image_overrides"]["radarr"], "custom:latest@sha256:existing")

    def test_source_refresh_does_not_change_pinned(self):
        cfg = {"image_overrides": {"radarr": "lscr.io/linuxserver/radarr:latest@sha256:old_tested_digest"}}
        result = ensure_pins(cfg, ["radarr"], {})
        self.assertFalse(result)
        self.assertEqual(cfg["image_overrides"]["radarr"],
                         "lscr.io/linuxserver/radarr:latest@sha256:old_tested_digest")

    def test_new_option_gets_base_pin(self):
        cfg: dict = {}
        result = ensure_pins(cfg, ["wud", "socket-proxy"], {})
        self.assertTrue(result)
        base, _ = load_tracking()
        self.assertEqual(cfg["image_overrides"]["wud"], base["wud"])
        self.assertEqual(cfg["image_overrides"]["socket-proxy"], base["socket-proxy"])


class TestDisplayPlan(unittest.TestCase):
    def test_returns_true_when_changed(self):
        plans = [UpdatePlan(service="radarr", tracking_ref="r:latest",
                            current_ref="r:latest@sha256:old", current_digest="sha256:old123456789ab",
                            new_digest="sha256:new123456789ab", new_ref="r:latest@sha256:new", changed=True)]
        self.assertTrue(display_plan(plans))

    def test_returns_false_when_all_current(self):
        plans = [UpdatePlan(service="radarr", tracking_ref="r:latest",
                            current_ref="r:latest@sha256:same", current_digest="sha256:same",
                            new_digest="sha256:same", new_ref="r:latest@sha256:same", changed=False)]
        self.assertFalse(display_plan(plans))


if __name__ == "__main__":
    unittest.main()
