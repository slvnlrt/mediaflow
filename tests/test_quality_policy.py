"""Regression tests for portable policy application and user-data preservation."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from mediaflow_lib.common import ProvisionError
from mediaflow_lib import quality_policy as policy


def leaf(name, number):
    return {"quality": {"id": number, "name": name}, "items": [], "allowed": True}


class QualityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "name": "Managed", "cutoff": "WEB", "upgradeAllowed": False,
            "minFormatScore": 0, "cutoffFormatScore": 0, "minUpgradeFormatScore": 1,
            "language": "Original", "formatScores": {"French": 1000, "Unwanted": -10000},
            "items": [
                {"quality": "SD", "allowed": False},
                {"group": "WEB", "members": ["WEB-DL", "WEBRip"], "allowed": True},
                {"quality": "Blu-ray", "allowed": True},
            ],
        }
        # Different order, grouping and IDs than the desired policy.
        self.template = [
            leaf("Blu-ray", 44), leaf("WEBRip", 33),
            {"id": 1070, "name": "Old group", "allowed": True,
             "items": [leaf("SD", 11), leaf("WEB-DL", 22)]},
            leaf("Future quality", 55),
        ]

    def test_reconstructs_order_groups_cutoff_and_disables_unknown_quality(self):
        original = copy.deepcopy(self.template)
        items = policy._build_items_from_template(self.template, self.profile)
        self.assertEqual([x.get("name") or x["quality"]["name"] for x in items],
                         ["SD", "WEB", "Blu-ray", "Future quality"])
        self.assertEqual([x["quality"]["id"] for x in items[1]["items"]], [22, 33])
        self.assertFalse(items[0]["allowed"])
        self.assertTrue(items[1]["allowed"])
        self.assertFalse(items[-1]["allowed"])
        self.assertEqual(policy._resolve_cutoff(items, "WEB"), items[1]["id"])
        self.assertEqual(policy._resolve_cutoff(items, "Blu-ray"), 44)
        self.assertEqual(self.template, original)

    def test_missing_required_quality_is_not_silently_ignored(self):
        with self.assertRaises(ProvisionError):
            policy._build_items_from_template([leaf("SD", 11)], self.profile)

    def test_preserves_unrelated_scores_and_profile_with_remapped_language(self):
        unrelated = {"id": 800, "name": "Personal profile", "items": self.template}
        managed = {
            "id": 900, "name": "Managed", "items": self.template,
            "formatItems": [
                {"format": 73, "name": "Personal format", "score": 750},
                {"format": 91, "name": "French", "score": 0},
            ],
        }
        applied = {}

        def request(method, path, payload=None):
            if method == "GET" and path.endswith("/qualityprofile"):
                return [copy.deepcopy(managed), copy.deepcopy(unrelated)]
            if method == "GET" and path.endswith("/language"):
                return [{"id": -80, "name": "Original"}, {"id": -90, "name": "Any"}]
            if method == "PUT":
                applied[path] = copy.deepcopy(payload)
                return payload
            raise AssertionError((method, path))

        api = Mock()
        api.request.side_effect = request
        with patch.dict(policy.POLICY, {"fixture": {"profiles": [self.profile]}}):
            result = policy.ensure_profiles(api, "fixture", {"French": 91, "Unwanted": 92})
        self.assertEqual(result, {"Managed": 900})
        self.assertEqual(set(applied), {"/api/v3/qualityprofile/900"})
        current = applied["/api/v3/qualityprofile/900"]
        self.assertEqual(current["language"], {"id": -80, "name": "Original"})
        self.assertEqual({x["format"]: x["score"] for x in current["formatItems"]},
                         {73: 750, 91: 1000, 92: -10000})

    def test_language_failure_does_not_fall_back_to_any(self):
        api = Mock()
        api.request.return_value = [{"id": -1, "name": "Any"}]
        with self.assertRaises(ProvisionError):
            policy.resolve_language(api, "Original")
        api.request.side_effect = ProvisionError("unreachable")
        with self.assertRaises(ProvisionError):
            policy.resolve_language(api, "French")

    def test_sizes_match_quality_not_user_display_title(self):
        api = Mock()
        api.request.return_value = [{
            "id": 70, "quality": {"id": 100, "name": "Blu-ray"},
            "title": "My custom title", "minSize": 0, "maxSize": 200, "preferredSize": 150,
        }]
        with patch.dict(policy.POLICY, {"fixture": {"qualityDefinitions": [{
            "quality": "Blu-ray", "minSize": 50, "maxSize": None, "preferredSize": None,
        }]}}):
            policy.ensure_quality_definitions(api, "fixture")
        method, path, payload = api.request.call_args.args
        self.assertEqual((method, path), ("PUT", "/api/v3/qualitydefinition/70"))
        self.assertEqual(payload["title"], "My custom title")
        self.assertEqual(payload["minSize"], 50)
        self.assertIsNone(payload["maxSize"])
        self.assertIsNone(payload["preferredSize"])

    def test_missing_size_definition_fails_before_mutation(self):
        api = Mock()
        api.request.return_value = []
        with patch.dict(policy.POLICY, {"fixture": {"qualityDefinitions": [{
            "quality": "Blu-ray", "minSize": 50, "maxSize": None, "preferredSize": None,
        }]}}):
            with self.assertRaises(ProvisionError):
                policy.ensure_quality_definitions(api, "fixture")
        self.assertEqual([call.args[0] for call in api.request.call_args_list], ["GET"])

    def test_existing_and_new_format_ids_are_resolved_without_overwriting_other_formats(self):
        definition = {
            "name": "Existing", "includeCustomFormatWhenRenaming": True,
            "specifications": [{
                "name": "Required marker", "implementation": "ReleaseTitleSpecification",
                "negate": True, "required": True, "fields": [{"name": "value", "value": r"\bREJECT\b"}],
            }],
        }
        created = dict(definition, name="New")
        writes = []

        def request(method, path, payload=None):
            if method == "GET":
                return [{"id": 400, "name": "Personal"}, {"id": 600, "name": "Existing"}]
            writes.append((method, path, copy.deepcopy(payload)))
            return dict(payload, id=700)

        api = Mock()
        api.request.side_effect = request
        with patch.dict(policy.POLICY, {"fixture": {"customFormats": [definition, created]}}):
            ids = policy.ensure_custom_formats(api, "fixture")
        self.assertEqual(ids, {"Existing": 600, "New": 700})
        self.assertEqual([(x[0], x[1]) for x in writes],
                         [("PUT", "/api/v3/customformat/600"), ("POST", "/api/v3/customformat")])
        spec = writes[0][2]["specifications"][0]
        self.assertTrue(spec["required"])
        self.assertTrue(spec["negate"])
        self.assertEqual(spec["fields"], [{"name": "value", "value": r"\bREJECT\b"}])


if __name__ == "__main__":
    unittest.main()
