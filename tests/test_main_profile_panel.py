from __future__ import annotations

import gc
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

from agent.profile import (
    DetectorDefinition,
    MeterDefinition,
    RuleDefinition,
    load_profile,
    save_profile,
)
from agent.rule_engine import SKILL_RULE_ACTION, RuleEngine, VisibilityRule
from agent.skills import ClickSkill, PressSkill, SkillPermissions
from main import PROFILE_NONE_TEXT, PersonalGameAIApp, collect_profile_contents
from vision.detector_registry import DetectorSpec
from vision.resource_bar import HSVRange


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _template(seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (24, 24, 3), dtype=np.uint8)


def _write_profile(root: Path, name: str = "Notepad demo") -> Path:
    return save_profile(
        root,
        name,
        detectors=[(DetectorDefinition("ok_button", "", 0.9, None), _template())],
        skills=[
            ClickSkill("press_ok", "ok_button", min_confidence=0.9),
            PressSkill("type_x", "x", enabled=True),
        ],
        rules=[
            RuleDefinition(
                VisibilityRule(
                    name="auto_ok",
                    detector_name="ok_button",
                    action=SKILL_RULE_ACTION,
                    min_confidence=0.9,
                    skill="press_ok",
                ),
                enabled=False,
            )
        ],
        permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
    )


class CollectProfileContentsTests(unittest.TestCase):
    def test_ui_click_rule_becomes_disabled_click_skill(self) -> None:
        templates = {"ok": (DetectorSpec("ok", threshold=0.9), _template())}
        engine = RuleEngine([VisibilityRule("r1", "ok", "click", min_confidence=0.85)])
        engine.disable_rule("r1")

        contents = collect_profile_contents(templates, engine, None)

        self.assertEqual(len(contents.detectors), 1)
        definition, image = contents.detectors[0]
        self.assertEqual((definition.name, definition.threshold), ("ok", 0.9))
        self.assertIs(image, templates["ok"][1])
        self.assertEqual(len(contents.skills), 1)
        skill = contents.skills[0]
        self.assertIsInstance(skill, ClickSkill)
        self.assertEqual((skill.name, skill.detector, skill.enabled), ("click_ok", "ok", False))
        self.assertEqual(skill.min_confidence, 0.85)
        (rule_definition,) = contents.rules
        self.assertEqual(rule_definition.rule.action, SKILL_RULE_ACTION)
        self.assertEqual(rule_definition.rule.skill, "click_ok")
        self.assertFalse(rule_definition.enabled)
        self.assertEqual(contents.skipped_rules, [])

    def test_matching_skill_is_reused_and_new_names_do_not_clash(self) -> None:
        templates = {"ok": (DetectorSpec("ok"), _template())}
        engine = RuleEngine(
            [
                VisibilityRule("same", "ok", "click", min_confidence=0.82),
                VisibilityRule("other", "ok", "click", min_confidence=0.95),
                VisibilityRule("third", "ok", "click", min_confidence=0.99),
            ]
        )

        contents = collect_profile_contents(templates, engine, None)

        self.assertEqual(
            [skill.name for skill in contents.skills], ["click_ok", "click_ok_2", "click_ok_3"]
        )
        self.assertEqual(
            [definition.rule.skill for definition in contents.rules],
            ["click_ok", "click_ok_2", "click_ok_3"],
        )

    def test_rules_without_a_saved_detector_are_skipped(self) -> None:
        engine = RuleEngine([VisibilityRule("lost", "gone", "click")])

        contents = collect_profile_contents({}, engine, None)

        self.assertEqual(contents.rules, [])
        self.assertEqual(contents.skills, [])
        self.assertEqual(contents.skipped_rules, ["lost"])

    def test_loaded_profile_skills_are_kept_but_orphan_click_skills_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = load_profile(_write_profile(Path(tmp)))

        contents = collect_profile_contents({}, profile.rule_engine(), profile)

        self.assertEqual([skill.name for skill in contents.skills], ["type_x"])
        self.assertEqual(contents.skipped_rules, ["auto_ok"])


class MainProfilePanelTests(unittest.TestCase):
    """v0.6 task 5: Profile panel Load / Save."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profiles_dir = Path(self._tmp.name)
        self.app.profiles_dir = self.profiles_dir
        self.messagebox = mock.patch("main.messagebox").start()
        self.askstring = mock.patch("main.simpledialog.askstring").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        self.app.capture = None
        self.app.close()
        # Collect Tk variables on the main thread (see test_main_planner_visibility).
        del self.app
        del self.root
        gc.collect()

    def _load(self, folder: str) -> None:
        self.app.refresh_profiles()
        self.app.profile_var.set(folder)
        self.app.load_selected_profile()

    def test_no_profile_means_no_key_permissions(self) -> None:
        self.assertIsNone(self.app.profile)
        self.assertIsNone(self.app._profile_permissions())
        self.assertEqual(self.app.profile_status_var.get(), PROFILE_NONE_TEXT)

    def test_refresh_lists_profile_folders(self) -> None:
        _write_profile(self.profiles_dir, "Game B")
        _write_profile(self.profiles_dir, "Game A")
        (self.profiles_dir / "not_a_profile").mkdir()

        self.app.refresh_profiles()

        self.assertEqual(list(self.app.profile_combo["values"]), ["game_a", "game_b"])
        self.assertEqual(self.app.profile_var.get(), "game_a")

    def test_load_swaps_detectors_rules_skills_and_permissions(self) -> None:
        _write_profile(self.profiles_dir)
        self.app.game_state.update_detector("stale", visible=True, confidence=0.9, bbox=(0, 0, 5, 5))

        self._load("notepad_demo")

        profile = self.app.profile
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "Notepad demo")
        self.assertEqual(self.app.registry.names, ("ok_button",))
        self.assertIn("ok_button", self.app._detector_templates)
        self.assertEqual([rule.name for rule in self.app.rule_engine.rules], ["auto_ok"])
        self.assertFalse(self.app.rule_engine.is_rule_enabled("auto_ok"))
        self.assertEqual(self.app.skill_book.names, ("press_ok", "type_x"))
        self.assertTrue(self.app.skill_book.is_enabled("type_x"))
        self.assertEqual(self.app._profile_permissions().allowed_keys, frozenset({"x"}))
        self.assertIsNone(self.app.game_state.get("stale"))
        self.assertTrue(self.app.vision_enabled_var.get())
        self.assertIn("Notepad demo", self.app.profile_status_var.get())
        self.assertIn("1 enabled in file", self.app.profile_status_var.get())
        self.messagebox.showerror.assert_not_called()

    def test_load_refused_while_input_control_is_on(self) -> None:
        _write_profile(self.profiles_dir)
        self.app.control_var.set(True)
        self.app._toggle_control()
        registry = self.app.registry

        self._load("notepad_demo")

        self.messagebox.showwarning.assert_called_once()
        self.assertIsNone(self.app.profile)
        self.assertIs(self.app.registry, registry)

    def test_load_stops_the_planner(self) -> None:
        _write_profile(self.profiles_dir)
        self.app.planner_enabled_var.set(True)
        with mock.patch.object(self.app.planner, "stop") as stop:
            self._load("notepad_demo")
        stop.assert_called()
        self.assertFalse(self.app.planner_enabled_var.get())
        self.assertIn("profile was loaded", self.app.planner_status_var.get())

    def test_bad_profile_changes_nothing(self) -> None:
        folder = _write_profile(self.profiles_dir)
        data = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
        data["skills"].append({"name": "stop", "type": "press", "key": "f8"})
        (folder / "profile.json").write_text(json.dumps(data), encoding="utf-8")
        registry = self.app.registry
        rule_engine = self.app.rule_engine

        self._load("notepad_demo")

        self.messagebox.showerror.assert_called_once()
        self.assertIsNone(self.app.profile)
        self.assertIsNone(self.app.skill_book)
        self.assertIs(self.app.registry, registry)
        self.assertIs(self.app.rule_engine, rule_engine)

    def test_unreadable_template_changes_nothing(self) -> None:
        folder = _write_profile(self.profiles_dir)
        (folder / "templates" / "ok_button.png").write_bytes(b"not a png")

        self._load("notepad_demo")

        self.messagebox.showerror.assert_called_once()
        self.assertIsNone(self.app.profile)
        self.assertEqual(self.app.registry.names, ())

    def test_vision_can_be_enabled_with_profile_detectors_only(self) -> None:
        _write_profile(self.profiles_dir)
        self._load("notepad_demo")
        self.app.vision_enabled_var.set(False)
        self.app.vision_enabled_var.set(True)

        self.app._toggle_vision()

        self.assertTrue(self.app.vision_enabled_var.get())
        self.messagebox.showwarning.assert_not_called()

    def _register_ui_detector(self, name: str = "ok") -> None:
        spec = DetectorSpec(name, threshold=0.9)
        template = _template()
        self.app.registry.register_array(spec, template)
        self.app._detector_templates[name] = (spec, template)

    def test_save_writes_ui_detectors_and_rules_as_a_loadable_profile(self) -> None:
        self._register_ui_detector()
        self.app.rule_engine.add_rule(VisibilityRule("click_when_ok", "ok", "click"))
        self.askstring.return_value = "My Game"

        self.app.save_current_profile()

        self.messagebox.showerror.assert_not_called()
        profile = load_profile(self.profiles_dir / "my_game")
        self.assertEqual(profile.name, "My Game")
        self.assertEqual([detector.name for detector in profile.detectors], ["ok"])
        (skill,) = profile.skills
        self.assertIsInstance(skill, ClickSkill)
        self.assertFalse(skill.enabled)
        self.assertEqual(profile.rules[0].rule.skill, skill.name)
        self.assertEqual(profile.permissions.allowed_keys, frozenset())
        self.assertEqual(self.app.profile_var.get(), "my_game")
        self.assertIn("my_game", self.app.profile_combo["values"])

    def test_save_with_nothing_to_save_warns(self) -> None:
        self.app.save_current_profile()

        self.messagebox.showwarning.assert_called_once()
        self.askstring.assert_not_called()
        self.assertEqual(list(self.profiles_dir.iterdir()), [])

    def test_save_cancelled_writes_nothing(self) -> None:
        self._register_ui_detector()
        self.askstring.return_value = None

        self.app.save_current_profile()

        self.assertEqual(list(self.profiles_dir.iterdir()), [])

    def test_existing_folder_needs_confirmation(self) -> None:
        folder = _write_profile(self.profiles_dir, "My Game")
        before = (folder / "profile.json").read_text(encoding="utf-8")
        self._register_ui_detector()
        self.askstring.return_value = "My Game"
        self.messagebox.askyesno.return_value = False

        self.app.save_current_profile()

        self.messagebox.askyesno.assert_called_once()
        self.assertEqual((folder / "profile.json").read_text(encoding="utf-8"), before)

        self.messagebox.askyesno.return_value = True
        self.app.save_current_profile()

        profile = load_profile(folder)
        self.assertEqual([detector.name for detector in profile.detectors], ["ok"])
        # Replaced, never deleted: the old template file is still there.
        self.assertTrue((folder / "templates" / "ok_button.png").is_file())

    def test_resave_keeps_loaded_profile_permissions_and_key_skills(self) -> None:
        _write_profile(self.profiles_dir)
        self._load("notepad_demo")
        self.askstring.return_value = "Copy"

        self.app.save_current_profile()

        copy = load_profile(self.profiles_dir / "copy")
        self.assertEqual(copy.permissions.allowed_keys, frozenset({"x"}))
        self.assertEqual([skill.name for skill in copy.skills], ["press_ok", "type_x"])
        self.assertEqual([definition.rule.name for definition in copy.rules], ["auto_ok"])
        self.assertFalse(copy.rules[0].enabled)

    def test_resave_preserves_loaded_profile_meters(self) -> None:
        meter = MeterDefinition(
            name="hp",
            roi=(2, 3, 40, 6),
            hsv_ranges=(HSVRange((50, 100, 100), (80, 255, 255)),),
            min_confidence=0.9,
        )
        save_profile(
            self.profiles_dir,
            "Meter Game",
            meters=[meter],
            skills=[PressSkill("type_x", "x", enabled=True)],
            permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
        )
        self._load("meter_game")
        self.askstring.return_value = "Meter Copy"

        self.app.save_current_profile()

        copy = load_profile(self.profiles_dir / "meter_copy")
        self.assertEqual(copy.meters, (meter,))

    def test_invalid_name_shows_error(self) -> None:
        self._register_ui_detector()
        self.askstring.return_value = "***"

        self.app.save_current_profile()

        self.messagebox.showerror.assert_called_once()
        self.assertEqual(list(self.profiles_dir.iterdir()), [])

    def test_clear_detectors_forgets_kept_templates(self) -> None:
        self._register_ui_detector()

        self.app.clear_detectors()

        self.assertEqual(self.app._detector_templates, {})


if __name__ == "__main__":
    unittest.main()
