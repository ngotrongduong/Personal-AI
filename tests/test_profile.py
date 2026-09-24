from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from agent.profile import (
    PROFILE_FILENAME,
    DetectorDefinition,
    ProfileError,
    RuleDefinition,
    list_profiles,
    load_profile,
    profile_slug,
    save_profile,
)
from agent.rule_engine import SKILL_RULE_ACTION, VisibilityRule
from agent.skills import ClickSkill, HoldSkill, PressSkill, SkillPermissions
from vision.detector_registry import DetectorRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]


def _template(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(12, 16, 3), dtype=np.uint8)


def _write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(encoded.tobytes())


def _valid_profile() -> dict:
    return {
        "format_version": 1,
        "name": "Test profile",
        "permissions": {
            "allowed_keys": ["x", "space"],
            "max_hold_seconds": 1.5,
            "max_actions_per_second": 5,
        },
        "detectors": [
            {"name": "ok_button", "template": "templates/ok_button.png", "threshold": 0.9,
             "roi": None}
        ],
        "skills": [
            {"name": "press_ok", "type": "click", "detector": "ok_button",
             "min_confidence": 0.9, "enabled": False},
            {"name": "type_x", "type": "press", "key": "x", "enabled": False},
            {"name": "hold_space", "type": "hold", "key": "space", "seconds": 1.0},
        ],
        "rules": [
            {"name": "auto_ok", "detector": "ok_button", "skill": "press_ok",
             "min_confidence": 0.9, "cooldown_seconds": 1.0, "enabled": True}
        ],
        "planner": {"enabled": False, "model": "qwen3.5:9b", "interval_seconds": 5.0},
    }


class ProfileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / "test"
        _write_png(self.folder / "templates" / "ok_button.png", _template())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, data: object) -> Path:
        (self.folder / PROFILE_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        return self.folder

    def assert_rejected(self, data: object, fragment: str = "") -> ProfileError:
        self.write(data)
        with self.assertRaises(ProfileError) as caught:
            load_profile(self.folder)
        if fragment:
            self.assertIn(fragment, str(caught.exception))
        return caught.exception


class LoadProfileTests(ProfileTestCase):
    def test_example_profile_loads(self) -> None:
        profile = load_profile(REPO_ROOT / "profiles" / "example")
        self.assertEqual(profile.name, "Notepad demo")
        self.assertEqual(profile.detectors, ())
        self.assertEqual(profile.permissions.allowed_keys, frozenset({"x", "space"}))
        book = profile.skill_book()
        self.assertEqual(book.names, ("type_x", "hold_space"))
        self.assertFalse(book.is_enabled("type_x"))
        self.assertFalse(book.is_enabled("hold_space"))
        self.assertFalse(profile.planner.enabled)

    def test_valid_profile_loads_everything(self) -> None:
        profile = load_profile(self.write(_valid_profile()))
        self.assertEqual(profile.detectors[0].template, "templates/ok_button.png")
        self.assertEqual(profile.detectors[0].threshold, 0.9)
        self.assertIsInstance(profile.skills[0], ClickSkill)
        self.assertIsInstance(profile.skills[1], PressSkill)
        self.assertIsInstance(profile.skills[2], HoldSkill)
        rule = profile.rules[0].rule
        self.assertEqual(rule.action, SKILL_RULE_ACTION)
        self.assertEqual(rule.skill, "press_ok")
        self.assertEqual(rule.detector_name, "ok_button")
        self.assertTrue(profile.rules[0].enabled)
        self.assertEqual(profile.planner.ollama.model, "qwen3.5:9b")  # type: ignore[union-attr]

    def test_disabled_rule_is_disabled_in_engine(self) -> None:
        data = _valid_profile()
        data["rules"][0]["enabled"] = False
        engine = load_profile(self.write(data)).rule_engine()
        self.assertFalse(engine.is_rule_enabled("auto_ok"))

    def test_missing_or_invalid_json(self) -> None:
        with self.assertRaises(ProfileError):
            load_profile(self.root / "nowhere")
        (self.folder / PROFILE_FILENAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(ProfileError):
            load_profile(self.folder)
        self.assert_rejected([1, 2], "object")

    def test_format_version_must_be_integer_one(self) -> None:
        for version in (2, "1", True, 1.0, None):
            with self.subTest(version=version):
                data = _valid_profile()
                data["format_version"] = version
                self.assert_rejected(data, "format_version")
        data = _valid_profile()
        del data["format_version"]
        self.assert_rejected(data, "format_version")

    def test_unknown_fields_rejected_at_every_level(self) -> None:
        paths = [
            (),
            ("permissions",),
            ("detectors", 0),
            ("skills", 0),
            ("skills", 1),
            ("skills", 2),
            ("rules", 0),
            ("planner",),
        ]
        for path in paths:
            with self.subTest(path=path):
                data = _valid_profile()
                target = data
                for part in path:
                    target = target[part]
                target["surprise"] = 1
                self.assert_rejected(data)

    def test_duplicate_names_rejected(self) -> None:
        for section in ("detectors", "skills", "rules"):
            with self.subTest(section=section):
                data = _valid_profile()
                data[section].append(copy.deepcopy(data[section][0]))
                self.assert_rejected(data, "Duplicate")

    def test_bad_references_rejected(self) -> None:
        data = _valid_profile()
        data["skills"][0]["detector"] = "missing"
        self.assert_rejected(data, "unknown detector")

        data = _valid_profile()
        data["rules"][0]["detector"] = "missing"
        self.assert_rejected(data, "unknown detector")

        data = _valid_profile()
        data["rules"][0]["skill"] = "missing"
        self.assert_rejected(data, "unknown skill")

    def test_template_path_must_stay_inside_profile(self) -> None:
        outside = self.root / "x.png"
        _write_png(outside, _template())
        for template in (
            "../x.png",
            "templates/../../x.png",
            str(outside),
            "C:/x.png",
            "/x.png",
            "\\x.png",
            "..\\x.png",
        ):
            with self.subTest(template=template):
                data = _valid_profile()
                data["detectors"][0]["template"] = template
                self.assert_rejected(data, "inside the profile folder")

    def test_template_must_be_existing_png(self) -> None:
        data = _valid_profile()
        data["detectors"][0]["template"] = "templates/missing.png"
        self.assert_rejected(data, "missing template")

        data = _valid_profile()
        data["detectors"][0]["template"] = "templates/ok_button.jpg"
        self.assert_rejected(data, ".png")

    def test_key_permissions_enforced_on_load(self) -> None:
        cases = {
            "forbidden key": ("skills", 1, "key", "f8"),
            "key outside allowlist": ("skills", 1, "key", "y"),
            "hold over max": ("skills", 2, "seconds", 2.0),
            "combo": ("skills", 1, "key", "ctrl+c"),
        }
        for label, (section, index, field, value) in cases.items():
            with self.subTest(case=label):
                data = _valid_profile()
                data[section][index][field] = value
                self.assert_rejected(data)

        data = _valid_profile()
        data["permissions"]["allowed_keys"].append("f8")
        self.assert_rejected(data, "permissions")

        data = _valid_profile()
        data["permissions"]["max_hold_seconds"] = 9
        self.assert_rejected(data, "permissions")

    def test_invalid_values_rejected(self) -> None:
        cases = [
            ("skills", 0, "type", "drag"),
            ("skills", 0, "type", ["click"]),
            ("skills", 1, "enabled", "yes"),
            ("rules", 0, "enabled", 1),
            ("rules", 0, "min_confidence", 2),
            ("rules", 0, "cooldown_seconds", -1),
            ("detectors", 0, "threshold", True),
            ("detectors", 0, "roi", [0, 0, 0, 10]),
            ("detectors", 0, "roi", [0, 0, 10]),
            ("detectors", 0, "name", "bad name"),
        ]
        for section, index, field, value in cases:
            with self.subTest(field=f"{section}.{field}", value=value):
                data = _valid_profile()
                data[section][index][field] = value
                self.assert_rejected(data)

    def test_invalid_planner_rejected(self) -> None:
        data = _valid_profile()
        data["planner"] = {"enabled": True}
        self.assert_rejected(data, "planner")

    def test_registry_gets_detectors_only_on_success(self) -> None:
        registry = DetectorRegistry()
        data = _valid_profile()
        data["rules"][0]["skill"] = "missing"
        self.write(data)
        with self.assertRaises(ProfileError):
            load_profile(self.folder, registry)
        self.assertEqual(registry.names, ())

        load_profile(self.write(_valid_profile()), registry)
        self.assertEqual(registry.names, ("ok_button",))

    def test_failed_registration_leaves_registry_clean(self) -> None:
        data = _valid_profile()
        data["detectors"].append(
            {"name": "second", "template": "templates/second.png", "threshold": 0.9}
        )
        _write_png(self.folder / "templates" / "second.png", _template(2))
        registry = DetectorRegistry()
        registry.register_array(
            DetectorDefinition("second", "t.png").spec(), _template(3)
        )
        self.write(data)
        with self.assertRaises(ProfileError):
            load_profile(self.folder, registry)
        self.assertEqual(registry.names, ("second",))


class SaveProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _save(self, **overrides: object) -> Path:
        options: dict[str, object] = {
            "detectors": [(DetectorDefinition("ok_button", "", 0.9, (0, 0, 50, 40)), _template())],
            "skills": [
                ClickSkill("press_ok", detector="ok_button", min_confidence=0.9),
                PressSkill("type_x", key="x"),
            ],
            "rules": [
                RuleDefinition(
                    VisibilityRule(
                        name="auto_ok",
                        detector_name="ok_button",
                        action=SKILL_RULE_ACTION,
                        skill="press_ok",
                        min_confidence=0.9,
                    ),
                    enabled=False,
                )
            ],
            "permissions": SkillPermissions(allowed_keys=frozenset({"x"})),
        }
        options.update(overrides)
        return save_profile(self.root, "My Game!", **options)  # type: ignore[arg-type]

    def test_round_trip(self) -> None:
        folder = self._save()
        self.assertEqual(folder, self.root / "my_game")
        self.assertTrue((folder / "templates" / "ok_button.png").is_file())

        registry = DetectorRegistry()
        profile = load_profile(folder, registry)
        self.assertEqual(profile.name, "My Game!")
        self.assertEqual(registry.names, ("ok_button",))
        self.assertEqual(profile.detectors[0].roi, (0, 0, 50, 40))
        self.assertEqual(profile.detectors[0].template, "templates/ok_button.png")
        self.assertEqual([skill.name for skill in profile.skills], ["press_ok", "type_x"])
        self.assertFalse(profile.rules[0].enabled)
        self.assertEqual(profile.rules[0].rule.skill, "press_ok")
        self.assertEqual(profile.permissions.allowed_keys, frozenset({"x"}))
        self.assertFalse(profile.planner.enabled)

        template = cv2.imdecode(
            np.frombuffer((folder / "templates" / "ok_button.png").read_bytes(), np.uint8),
            cv2.IMREAD_COLOR,
        )
        np.testing.assert_array_equal(template, _template())

    def test_refuses_to_overwrite_without_flag(self) -> None:
        folder = self._save()
        before = (folder / PROFILE_FILENAME).read_text(encoding="utf-8")
        with self.assertRaises(ProfileError):
            self._save(skills=[], rules=[])
        self.assertEqual((folder / PROFILE_FILENAME).read_text(encoding="utf-8"), before)

        self._save(skills=[], rules=[], overwrite=True)
        self.assertEqual(load_profile(folder).skills, ())
        # Overwriting replaces files but never deletes the old template.
        self.assertTrue((folder / "templates" / "ok_button.png").is_file())

    def test_invalid_profile_writes_nothing(self) -> None:
        cases = {
            "unpermitted key": {"skills": [PressSkill("type_y", key="y")], "rules": []},
            "dangling rule": {"skills": [PressSkill("type_x", key="x")]},
            "empty template": {
                "detectors": [(DetectorDefinition("ok_button", ""), np.zeros((0, 0, 3), np.uint8))]
            },
            "bad detector name": {
                "detectors": [(DetectorDefinition("../evil", ""), _template())],
                "skills": [],
                "rules": [],
            },
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ProfileError):
                    self._save(**overrides)
                self.assertFalse((self.root / "my_game").exists())

    def test_non_skill_rule_cannot_be_saved(self) -> None:
        rule = VisibilityRule(name="plain", detector_name="ok_button", action="click")
        with self.assertRaises(ProfileError):
            self._save(rules=[RuleDefinition(rule)])

    def test_slug(self) -> None:
        self.assertEqual(profile_slug("  My Game! 2 "), "my_game_2")
        self.assertEqual(profile_slug("../../etc"), "etc")
        self.assertEqual(len(profile_slug("a" * 100)), 64)
        for name in ("", "!!!", "..", "   "):
            with self.subTest(name=name):
                with self.assertRaises(ProfileError):
                    profile_slug(name)


class ListProfilesTests(unittest.TestCase):
    def test_lists_folders_with_profile_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(list_profiles(root / "missing"), [])
            for name in ("beta", "alpha"):
                (root / name).mkdir()
                (root / name / PROFILE_FILENAME).write_text("{}", encoding="utf-8")
            (root / "empty").mkdir()
            (root / "file.txt").write_text("x", encoding="utf-8")
            self.assertEqual(list_profiles(root), ["alpha", "beta"])

    def test_repo_example_is_listed(self) -> None:
        self.assertIn("example", list_profiles(REPO_ROOT / "profiles"))


if __name__ == "__main__":
    unittest.main()
