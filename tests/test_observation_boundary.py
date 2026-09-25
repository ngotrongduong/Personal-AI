"""v1.0 agent invariant: observation never adds input.

The observation modules (effects, agent runs) may read state, the profile and
the clock, but must never reach the skill, rule, autopilot, executor,
dispatcher or input modules, not even through another module.
"""

from __future__ import annotations

import unittest

from tests.test_memory_boundary import ROOT, dynamic_imports, imported_modules, reachable_modules


OBSERVATION_MODULES = ("agent/skill_effects.py", "agent/agent_session.py")
FORBIDDEN = frozenset(
    {
        "agent.skills",
        "agent.rule_engine",
        "agent.autopilot",
        "agent.skill_executor",
        "agent.action_dispatcher",
        "agent.proposal_mailbox",
        "agent.planner_controller",
        "agent.llm_planner",
        "core.input_controller",
        "pydirectinput",
        "pynput",
    }
)


class ObservationBoundaryTests(unittest.TestCase):
    def test_observation_modules_never_reach_the_input_path(self) -> None:
        for module in OBSERVATION_MODULES:
            self.assertTrue((ROOT / module).exists(), module)
            with self.subTest(module=module):
                reached = reachable_modules(module) | imported_modules(module)
                self.assertEqual(reached & FORBIDDEN, set())
                self.assertEqual({name for name in reached if name.startswith("core.")}, set())
                self.assertEqual(dynamic_imports(module), [])

    def test_the_forbidden_set_is_really_on_the_input_path(self) -> None:
        # Guards the check itself: the dispatcher does reach these names.
        reached = reachable_modules("agent/action_dispatcher.py") | imported_modules(
            "agent/action_dispatcher.py"
        )
        self.assertIn("agent.skills", reached)
        self.assertIn("core.input_controller", reached)


if __name__ == "__main__":
    unittest.main()
