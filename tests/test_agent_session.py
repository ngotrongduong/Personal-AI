"""v1.0 agent runs: preflight, run budget, goal condition, planner fields, check_model."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError

from agent.agent_session import (
    GOAL_FRESH_SECONDS,
    STOP_BUDGET,
    STOP_GOAL,
    AgentRun,
    GoalCondition,
    GoalConditionError,
    PreflightFacts,
    RunBudget,
    parse_goal_condition,
    run_preflight,
)
from agent.game_state import GameState
from agent.ollama_client import OllamaClient, OllamaClientConfig, OllamaErrorKind
from agent.planner_config import PlannerConfig, load_planner_config
from agent.profile import PROFILE_FILENAME, ProfileError, load_profile, save_profile
from agent.skills import PressSkill, SkillPermissions
from tests.test_profile import ProfileTestCase, _template, _valid_profile


def _facts(**changes: object) -> PreflightFacts:
    facts = PreflightFacts(
        profile_name="Notepad",
        capture_running=True,
        window_title="Untitled - Notepad",
        planner_configured=True,
        model="qwen3.5:9b",
        ollama_ok=True,
        ollama_detail="model 'qwen3.5:9b' is available",
        enabled_skills=("type_x",),
        input_enabled=True,
        goal="type an x",
    )
    return replace(facts, **changes)


class PreflightTests(unittest.TestCase):
    def test_all_checks_pass(self) -> None:
        report = run_preflight(_facts())
        self.assertTrue(report.ready)
        self.assertEqual(report.failures, ())
        self.assertEqual(report.summary(), "Ready")
        self.assertEqual(
            [check.name for check in report.checks],
            ["Profile", "Capture", "Planner settings", "Ollama", "Enabled skills",
             "Input control", "Goal"],
        )
        self.assertTrue(all(line.startswith("[OK]") for line in
                            (check.line() for check in report.checks)))

    def test_each_required_check_blocks_the_start(self) -> None:
        for changes, failing in [
            ({"profile_name": None}, "Profile"),
            ({"capture_running": False}, "Capture"),
            ({"planner_configured": False, "model": None}, "Planner settings"),
            ({"ollama_ok": False, "ollama_detail": "Could not reach Ollama"}, "Ollama"),
            ({"enabled_skills": ()}, "Enabled skills"),
        ]:
            with self.subTest(failing=failing):
                report = run_preflight(_facts(**changes))
                self.assertFalse(report.ready)
                self.assertIn(failing, [check.name for check in report.failures])
                self.assertTrue(report.summary().startswith("Not ready: "))

    def test_ollama_fails_without_planner_settings(self) -> None:
        report = run_preflight(_facts(planner_configured=False, ollama_ok=True))
        names = [check.name for check in report.failures]
        self.assertEqual(names, ["Planner settings", "Ollama"])

    def test_ollama_detail_is_shown(self) -> None:
        report = run_preflight(_facts(ollama_ok=False, ollama_detail="Model 'm' is not installed"))
        self.assertIn("Model 'm' is not installed", report.summary())
        ollama = [check for check in report.checks if check.name == "Ollama"][0]
        self.assertTrue(ollama.line().startswith("[FAIL]"))

    def test_advisory_checks_do_not_block(self) -> None:
        report = run_preflight(_facts(input_enabled=False, goal="  "))
        self.assertTrue(report.ready)
        self.assertEqual(report.summary(), "Ready (check: Input control, Goal)")
        notes = [check.line() for check in report.checks if not check.ok]
        self.assertTrue(all(line.startswith("[NOTE]") for line in notes))


class RunBudgetTests(unittest.TestCase):
    def test_remaining_and_expiry(self) -> None:
        budget = RunBudget(max_seconds=60.0, started_at=100.0)
        self.assertEqual(budget.remaining(130.0), 30.0)
        self.assertFalse(budget.expired(159.9))
        self.assertTrue(budget.expired(160.0))
        self.assertEqual(budget.remaining(500.0), 0.0)

    def test_cap_is_enforced(self) -> None:
        RunBudget(max_seconds=120 * 60, started_at=0.0)
        for bad in (0.0, -1.0, 120 * 60 + 1, float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RunBudget(max_seconds=bad, started_at=0.0)


class GoalConditionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.goal = GoalCondition("done")

    def test_met_by_a_fresh_observation_after_the_start(self) -> None:
        self.state.update_detector("done", visible=True, confidence=0.9, observed_at=10.5)
        self.assertTrue(self.goal.met(self.state, started_at=10.0, now=11.0))

    def test_not_met_before_the_start_or_when_stale(self) -> None:
        self.state.update_detector("done", visible=True, confidence=0.9, observed_at=9.0)
        self.assertFalse(self.goal.met(self.state, started_at=10.0, now=9.5))
        self.state.update_detector("done", visible=True, confidence=0.9, observed_at=10.0)
        self.assertFalse(self.goal.met(self.state, started_at=10.0, now=10.2))
        self.state.update_detector("done", visible=True, confidence=0.9, observed_at=11.0)
        self.assertFalse(
            self.goal.met(self.state, started_at=10.0, now=11.0 + GOAL_FRESH_SECONDS + 0.1)
        )

    def test_confidence_and_visibility(self) -> None:
        self.state.update_detector("done", visible=True, confidence=0.5, observed_at=11.0)
        self.assertFalse(self.goal.met(self.state, started_at=10.0, now=11.0))
        gone = GoalCondition("done", visible=False)
        self.assertTrue(gone.met(self.state, started_at=10.0, now=11.0))

    def test_missing_detector_is_not_met(self) -> None:
        self.assertFalse(self.goal.met(self.state, started_at=0.0, now=1.0))

    def test_parse(self) -> None:
        self.assertEqual(
            parse_goal_condition({"detector": "done", "visible": False, "min_confidence": 0.7}),
            GoalCondition("done", False, 0.7),
        )
        for block, fragment in [
            ({"visible": True}, "detector"),
            ({"detector": "done", "within_seconds": 1}, "unknown field"),
            ({"detector": "done", "min_confidence": 2}, "min_confidence"),
            ({"detector": "done", "visible": "yes"}, "visible"),
            ("done", "object"),
        ]:
            with self.subTest(block=block):
                with self.assertRaises(GoalConditionError) as caught:
                    parse_goal_condition(block)
                self.assertIn(fragment, str(caught.exception))


class AgentRunTests(unittest.TestCase):
    def test_budget_stop(self) -> None:
        run = AgentRun(RunBudget(60.0, 100.0))
        state = GameState()
        self.assertIsNone(run.stop_reason(state, 159.0))
        self.assertEqual(run.stop_reason(state, 160.0), STOP_BUDGET)

    def test_goal_stop_wins_over_budget(self) -> None:
        run = AgentRun(RunBudget(60.0, 100.0), GoalCondition("done"))
        state = GameState()
        state.update_detector("done", visible=True, confidence=0.9, observed_at=159.5)
        self.assertEqual(run.stop_reason(state, 160.0), STOP_GOAL)

    def test_status_line(self) -> None:
        run = AgentRun(RunBudget(15 * 60.0, 0.0), GoalCondition("done"))
        run.note_step()
        run.note_effect("confirmed")
        run.note_effect("not_seen")
        run.note_effect("none")
        line = run.status_line(65.0)
        self.assertIn("13:55 left", line)
        self.assertIn("1 step(s)", line)
        self.assertIn("1 confirmed / 1 not seen", line)
        self.assertIn("goal: done visible", line)
        self.assertIn("goal: none", AgentRun(RunBudget(60.0, 0.0)).status_line(0.0))


class PlannerRunFieldTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = load_planner_config({"planner": {"enabled": False}})
        self.assertEqual(config.max_run_minutes, 15.0)
        self.assertIsNone(config.stop_when)

    def test_parse(self) -> None:
        config = load_planner_config(
            {"planner": {"max_run_minutes": 30, "stop_when": {"detector": "done"}}}
        )
        self.assertEqual(config.max_run_minutes, 30.0)
        self.assertEqual(config.stop_when, GoalCondition("done"))

    def test_bad_values_rejected(self) -> None:
        for block in (
            {"max_run_minutes": 0},
            {"max_run_minutes": 121},
            {"max_run_minutes": True},
            {"max_run_minutes": "15"},
            {"stop_when": {"detector": "done", "extra": 1}},
            {"stop_when": []},
        ):
            with self.subTest(block=block), self.assertRaises(ValueError):
                load_planner_config({"planner": block})
        with self.assertRaises(ValueError):
            PlannerConfig(max_run_minutes=500)


class ProfileRunFieldTests(ProfileTestCase):
    def test_stop_when_must_name_a_declared_detector(self) -> None:
        data = _valid_profile()
        data["planner"] = {"stop_when": {"detector": "nope"}}
        self.assert_rejected(data, "unknown detector")
        data["planner"] = {"stop_when": {"detector": "ok_button"}, "max_run_minutes": 5}
        profile = load_profile(self.write(data))
        self.assertEqual(profile.planner.stop_when, GoalCondition("ok_button"))
        self.assertEqual(profile.planner.max_run_minutes, 5.0)

    def test_bad_max_run_minutes_rejected(self) -> None:
        data = _valid_profile()
        data["planner"] = {"max_run_minutes": 240}
        self.assert_rejected(data, "max_run_minutes")

    def test_save_round_trips_run_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from agent.profile import DetectorDefinition

            planner = PlannerConfig(max_run_minutes=20, stop_when=GoalCondition("ok_button", False))
            folder = save_profile(
                tmp,
                "Runs",
                detectors=[(DetectorDefinition("ok_button", "", 0.9), _template())],
                skills=[PressSkill("type_x", "x")],
                permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
                planner=planner,
            )
            written = json.loads((Path(folder) / PROFILE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(written["planner"]["max_run_minutes"], 20)
            self.assertEqual(
                written["planner"]["stop_when"],
                {"detector": "ok_button", "visible": False, "min_confidence": 0.8},
            )
            loaded = load_profile(folder).planner
            self.assertEqual(loaded.max_run_minutes, 20.0)
            self.assertEqual(loaded.stop_when, GoalCondition("ok_button", False))

    def test_save_rejects_stop_when_on_an_unknown_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProfileError):
                save_profile(
                    tmp,
                    "Runs",
                    skills=[PressSkill("type_x", "x")],
                    permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
                    planner=PlannerConfig(stop_when=GoalCondition("ghost")),
                )


class CheckModelTests(unittest.TestCase):
    def client(self, transport, model: str = "qwen3.5:9b") -> OllamaClient:
        return OllamaClient(
            OllamaClientConfig(model=model, host="planner.local", port=22334, timeout_seconds=3),
            transport=transport,
        )

    def test_model_listed(self) -> None:
        requests = []

        def transport(request, timeout):
            requests.append((request, timeout))
            return json.dumps({"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b"}]}).encode()

        result = self.client(transport).check_model()
        self.assertTrue(result.successful)
        self.assertIn("qwen3.5:9b", result.text or "")
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://planner.local:22334/api/tags")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 3.0)

    def test_untagged_model_matches_latest(self) -> None:
        def transport(_request, _timeout):
            return json.dumps({"models": [{"name": "llama3:latest"}]}).encode()

        self.assertTrue(self.client(transport, model="llama3").check_model().successful)

    def test_model_missing(self) -> None:
        def transport(_request, _timeout):
            return json.dumps({"models": [{"name": "other:1b"}]}).encode()

        result = self.client(transport).check_model()
        assert result.error is not None
        self.assertEqual(result.error.kind, OllamaErrorKind.MODEL_MISSING)
        self.assertIn("ollama pull qwen3.5:9b", result.error.message)

    def test_failures_are_structured(self) -> None:
        def unreachable(_request, _timeout):
            raise URLError(socket.gaierror("name lookup failed"))

        def http_error(request, _timeout):
            raise HTTPError(request.full_url, 500, "boom", None, None)

        def not_json(_request, _timeout):
            return b"<html>"

        def no_models(_request, _timeout):
            return b'{"other": 1}'

        for transport, kind in [
            (unreachable, OllamaErrorKind.CONNECTION),
            (http_error, OllamaErrorKind.HTTP_STATUS),
            (not_json, OllamaErrorKind.RESPONSE_FORMAT),
            (no_models, OllamaErrorKind.RESPONSE_FORMAT),
        ]:
            with self.subTest(kind=kind):
                result = self.client(transport).check_model()
                assert result.error is not None
                self.assertEqual(result.error.kind, kind)


if __name__ == "__main__":
    unittest.main()
