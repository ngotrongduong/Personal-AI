from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
import math
import queue
import threading
import time
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import cv2
import numpy as np
from PIL import Image, ImageTk
from pynput import keyboard
import win32gui

from agent.action_dispatcher import ActionDispatcher
from agent.agent_session import (
    DEFAULT_MAX_RUN_MINUTES,
    AgentRun,
    PreflightFacts,
    PreflightReport,
    RunBudget,
    run_preflight,
)
from agent.autopilot import (
    DEFAULT_AUTO_MAX_STEPS,
    DEFAULT_TTL_SECONDS,
    HARD_MAX_AUTO_STEPS,
    MAX_CONSECUTIVE_FAILURES,
    Autopilot,
    OfferResult,
    validate_auto_max_steps,
)
from agent.game_state import GameState
from agent.llm_planner import MAX_GOAL_LENGTH, SkillBookCatalog
from agent.memory_store import check_slug, new_session_path, notes_path
from agent.notes import (
    MAX_LLM_NOTES,
    MAX_NOTES,
    Note,
    NoteBook,
    NoteResult,
    NotesError,
    load_notes,
    save_notes,
)
from agent.ollama_client import OllamaClient, OllamaClientConfig, OllamaResult
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.planner_scheduler import PlannerCycleReport
from agent.proposal_mailbox import ProposalMailbox, SkillProposal
from agent.profile import (
    DetectorDefinition,
    GameProfile,
    ProfileError,
    RuleDefinition,
    list_profiles,
    load_profile,
    profile_slug,
    read_templates,
    save_profile,
)
from agent.rule_engine import SKILL_RULE_ACTION, ActionIntent, RuleEngine, VisibilityRule
from agent.session_log import SessionLogWriter
from agent.skill_effects import EFFECT_NOT_SEEN, EFFECT_PENDING, EffectWatch, Expectation
from agent.skill_executor import SkillExecutor
from agent.skills import ClickSkill, HoldSkill, PressSkill, Skill, SkillBook, SkillPermissions
from agent.step_history import Decision, StepHistory, StepRecord
from agent.vision_state_bridge import apply_detections
from core.capture import WindowCapture
from core.input_controller import InputController
from core.window_utils import list_visible_windows, client_region, focus_window, is_foreground
from recording.recorder_controller import (
    DEFAULT_FPS as RECORDING_DEFAULT_FPS,
    MAX_FPS as RECORDING_MAX_FPS,
    MIN_FPS as RECORDING_MIN_FPS,
    STOP_USER as RECORDING_STOP_USER,
    RecordingController,
    RecordingRefusedError,
    RecordingStatus,
    validate_fps,
)
from vision.detector_registry import DetectorRegistry, DetectorSpec
from vision.template_matcher import TemplateMatcher, MatchResult


APP_VERSION = "0.8.0"
PLANNER_DEFAULT_MODEL = "qwen3.5:9b"
PLANNER_NO_CYCLE_TEXT = "Last cycle: —"
PLANNER_MESSAGE_MAX_CHARS = 100
PLANNER_NO_PROPOSAL_TEXT = "Proposal: none."
PLANNER_APPROVE_MODE_TEXT = "Mode: approve each step."
# Executor source of planner steps; their results feed the autopilot and history.
PLANNER_SOURCE = "planner"

MEMORY_NO_SESSION_TEXT = "Session log: none yet (a session starts with the planner)."
SESSION_END_PLANNER_OFF = "planner disabled"
SESSION_END_RESTART = "planner restarted"
SESSION_END_F8 = "emergency stop"
SESSION_END_APP_CLOSE = "app closed"
SESSION_END_AGENT_STOP = "agent stopped"

# v1.0 Agent panel.
AGENT_IDLE_TEXT = "Agent: press Preflight to check everything a run needs."
AGENT_NO_CHECKS_TEXT = "Checks: not run yet."
AGENT_NO_RUN_TEXT = "Run: none. A run starts with the planner and ends with it."

RECORDING_IDLE_TEXT = "Recording: off."
RECORDING_STOP_F8 = "f8"
RECORDING_STOP_APP_CLOSE = "app_close"
RECORDING_STOP_CAPTURE = "capture_stopped"
RECORDING_STOP_INPUT_CONTROL = "input_control_enabled"
# Bounded wait for the writer to finalise session.json when the app closes.
RECORDING_CLOSE_WAIT_SECONDS = 3.0

PROFILE_NONE_TEXT = "Profile: none loaded. Key skills stay blocked until a profile is loaded."
SKILLS_NONE_TEXT = "Skills: none. Load a profile to list its skills."
SKILLS_PER_ROW = 2
SKILL_NO_RESULT_TEXT = "—"


def describe_skill(skill: Skill | None) -> str:
    """Short, display-only summary of what a skill does."""
    if isinstance(skill, ClickSkill):
        return f"click {skill.detector}"
    if isinstance(skill, PressSkill):
        return f"press {skill.key}"
    if isinstance(skill, HoldSkill):
        return f"hold {skill.key} {skill.seconds:g}s"
    return "unknown"


@dataclass(frozen=True, slots=True)
class ProfileContents:
    """What Save Profile writes, built from the app's current detectors and rules."""

    detectors: list[tuple[DetectorDefinition, np.ndarray]]
    skills: list[Skill]
    rules: list[RuleDefinition]
    skipped_rules: list[str]


def collect_profile_contents(
    detector_templates: dict[str, tuple[DetectorSpec, np.ndarray]],
    rule_engine: RuleEngine,
    profile: GameProfile | None,
) -> ProfileContents:
    """Turn the current detectors and rules into profile contents.

    - Every detector with a kept template image is saved.
    - The loaded profile's skills are kept as written in its file (runtime
      toggles are not saved); click skills whose detector is gone are dropped.
    - A UI click rule becomes a rule that fires a click skill on the same
      detector. New click skills are saved **disabled**, so a saved rule never
      clicks after loading until the user enables its skill.
    - Rules whose detector or skill is not saved are skipped and reported.
    """

    detectors = [
        (DetectorDefinition(name, "", spec.threshold, spec.roi), template)
        for name, (spec, template) in detector_templates.items()
    ]
    detector_names = set(detector_templates)
    skills: list[Skill] = [
        skill
        for skill in (profile.skills if profile is not None else ())
        if not isinstance(skill, ClickSkill) or skill.detector in detector_names
    ]
    skill_names = {skill.name for skill in skills}

    rules: list[RuleDefinition] = []
    skipped: list[str] = []
    for rule in rule_engine.rules:
        enabled = rule_engine.is_rule_enabled(rule.name)
        if rule.detector_name not in detector_names:
            skipped.append(rule.name)
            continue
        if rule.skill is not None:
            if rule.skill not in skill_names:
                skipped.append(rule.name)
                continue
            rules.append(RuleDefinition(rule, enabled))
            continue
        if rule.action != "click":
            skipped.append(rule.name)
            continue
        skill_name = _click_skill_for(rule, skills, skill_names)
        rules.append(
            RuleDefinition(
                VisibilityRule(
                    name=rule.name,
                    detector_name=rule.detector_name,
                    action=SKILL_RULE_ACTION,
                    min_confidence=rule.min_confidence,
                    max_observation_age_seconds=rule.max_observation_age_seconds,
                    cooldown_seconds=rule.cooldown_seconds,
                    skill=skill_name,
                ),
                enabled,
            )
        )
    return ProfileContents(detectors, skills, rules, skipped)


def _click_skill_for(rule: VisibilityRule, skills: list[Skill], skill_names: set[str]) -> str:
    """Reuse a matching click skill, or add a new disabled one for `rule`."""

    for skill in skills:
        if (
            isinstance(skill, ClickSkill)
            and skill.detector == rule.detector_name
            and skill.min_confidence == rule.min_confidence
            and skill.max_observation_age_seconds == rule.max_observation_age_seconds
        ):
            return skill.name
    base = f"click_{rule.detector_name}"[:60]
    name = base
    suffix = 2
    while name in skill_names:
        name = f"{base}_{suffix}"
        suffix += 1
    skills.append(
        ClickSkill(
            name=name,
            detector=rule.detector_name,
            min_confidence=rule.min_confidence,
            max_observation_age_seconds=rule.max_observation_age_seconds,
            enabled=False,
        )
    )
    skill_names.add(name)
    return name


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_count(value: int) -> str:
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def format_recording_status(status: RecordingStatus) -> str:
    """One-line, display-only text for the Recording panel."""
    counts = (
        f"{status.frames} frames · {status.dropped} dropped · "
        f"{_format_count(status.events)} events"
    )
    if status.state == "recording":
        return f"● REC {_format_duration(status.elapsed)} · {counts}"
    if status.state == "stopping":
        return f"Stopping recording ({status.stop_reason}) · {counts}"
    if status.state == "stopped":
        name = status.session_dir.name if status.session_dir is not None else "?"
        return (
            f"Recording saved: recordings/{name} · {_format_duration(status.elapsed)} · "
            f"{counts} · stop: {status.stop_reason}"
        )
    if status.state == "failed":
        return f"Recording failed: {status.error}"
    return f"Recording: {status.state}"


class _PlannerLogHandler(logging.Handler):
    """Queue scheduler-thread planner logs for the Tk loop (never calls into Tk)."""

    def __init__(self, app: PersonalGameAIApp) -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        if not self._app._closing:
            self._app._planner_queue.put(("log", 0, message))


def default_memory_root() -> Path:
    """The folder that holds ``memory/``. Tests point it at a temporary folder."""

    return Path(__file__).resolve().parent


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """(modified time, size) of a file; None when it is missing or unreadable."""

    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


class PersonalGameAIApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Personal Game AI v{APP_VERSION}")
        self.root.geometry("1220x900")
        self.root.minsize(940, 700)

        self.base_dir = Path(__file__).resolve().parent
        self.templates_dir = self.base_dir / "templates"
        self.snapshots_dir = self.base_dir / "snapshots"
        # Created by the recording thread on first use, never on the Tk thread.
        self.recordings_dir = self.base_dir / "recordings"
        self.templates_dir.mkdir(exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)

        self.windows = []
        self.capture: WindowCapture | None = None
        self.input = InputController()
        self.matcher = TemplateMatcher(threshold=0.82)
        self.registry = DetectorRegistry()
        self.game_state = GameState()
        self._registry_visibility: dict[str, bool] = {}
        self.rule_engine = RuleEngine()
        # v0.6 profiles. Profiles are read and written only on the Tk thread;
        # the dispatcher reads the loaded profile's permissions on every key
        # action, so without a profile no key can be sent.
        self.profiles_dir = self.base_dir / "profiles"
        self.profile: GameProfile | None = None
        self.skill_book: SkillBook | None = None
        # Settings and template image of every registered detector, kept so
        # Save Profile can write them.
        self._detector_templates: dict[str, tuple[DetectorSpec, np.ndarray]] = {}
        self.dispatcher = ActionDispatcher(
            self.input, permissions_provider=self._profile_permissions
        )
        # Skills (manual Run and skill rules) run one at a time on the
        # executor's worker thread, so a hold never blocks Tk. Results come
        # back through executor.drain() in _poll_preview.
        self.executor = SkillExecutor(self.dispatcher)
        self.planner = PlannerController(self.game_state)
        # Polled on the recording thread: read the plain bool, never a Tk variable.
        self.recorder = RecordingController(
            app_version=APP_VERSION,
            input_control_enabled=lambda: self.input.enabled,
        )

        self.preview_photo = None
        self.preview_image_item = None
        self.latest_raw_frame = None
        self.latest_match: MatchResult | None = None

        self._closing = False
        self._last_vision_time = 0.0
        self._vision_interval = 0.10  # 10 Hz template matching

        # Preview transform from source pixels -> canvas pixels
        self.preview_scale = 1.0
        self.preview_offset_x = 0
        self.preview_offset_y = 0
        self.preview_source_w = 0
        self.preview_source_h = 0

        # Selection state
        self.selection_mode = False
        self.selection_start = None
        self.selection_rect_id = None

        self.window_var = tk.StringVar()
        self.status_var = tk.StringVar(value="READY")
        self.last_event_var = tk.StringVar(value="Last event: startup complete")
        self.fps_var = tk.StringVar(value="Capture: 0.0 FPS")
        self.control_var = tk.BooleanVar(value=False)

        self.vision_enabled_var = tk.BooleanVar(value=False)
        self.threshold_var = tk.DoubleVar(value=0.82)
        self.vision_var = tk.StringVar(value="Vision: no template loaded")
        self.template_var = tk.StringVar(value="Template: none")
        self.detector_name_var = tk.StringVar(value="detector_1")
        self.detectors_var = tk.StringVar(value="Detectors: none registered")

        self.profile_var = tk.StringVar(value="")
        self.profile_status_var = tk.StringVar(value=PROFILE_NONE_TEXT)
        self.skills_status_var = tk.StringVar(value=SKILLS_NONE_TEXT)
        # Skill name -> (Enabled checkbox variable, last-result text), one per
        # row of the Skills panel; rebuilt whenever a profile is loaded.
        self._skill_rows: dict[str, tuple[tk.BooleanVar, tk.StringVar]] = {}
        # Name of the skill last submitted; it is the running one while
        # executor.busy (Tk thread only).
        self._running_skill: str | None = None

        self.rule_name_var = tk.StringVar(value="rule_1")
        self.rule_detector_var = tk.StringVar(value="")
        self.rule_min_confidence_var = tk.DoubleVar(value=0.82)
        self.rules_var = tk.StringVar(value="Rules: 0 active. Input control still gates every dispatch.")

        self.planner_enabled_var = tk.BooleanVar(value=False)
        self.planner_model_var = tk.StringVar(value=PLANNER_DEFAULT_MODEL)
        self.planner_interval_var = tk.StringVar(value="5.0")
        self.planner_status_var = tk.StringVar(value="Planner: disabled.")
        self.planner_last_cycle_var = tk.StringVar(value=PLANNER_NO_CYCLE_TEXT)
        # Bumped on every planner start/stop (Tk thread only) so cycle reports
        # from a stopped or replaced scheduler are never displayed.
        self._planner_generation = 0
        # Cycle reports and log lines from the scheduler thread, drained by
        # _poll_preview: ("cycle", generation, report) or ("log", 0, message).
        self._planner_queue: queue.SimpleQueue[tuple[str, int, object]] = queue.SimpleQueue()
        # v0.7 closed loop. The planner thread only posts proposals to the
        # mailbox; the Tk thread decides (Autopilot) and submits the skill.
        self.proposals = ProposalMailbox()
        self.step_history = StepHistory()
        self.autopilot = Autopilot()
        # How the running planner step was decided ("approved" or "auto").
        self._planner_decision: Decision | None = None
        # The intent submitted for that step; its drained run completes it.
        self._planner_intent: ActionIntent | None = None
        # The window auto mode was confirmed for; auto steps run nowhere else.
        self._auto_hwnd: int | None = None
        # v1.0 observed effect of the last planner step: at most one watch,
        # polled on the Tk thread (_poll_effect_watch). The step's history
        # record is kept to fill in its effect. The Event lets the scheduler
        # thread's gate skip the LLM call while a watch is pending.
        self._effect_watch: EffectWatch | None = None
        self._effect_record: StepRecord | None = None
        self._effect_pending = threading.Event()
        # v1.0 agent runs. Every planner session is a run with a budget and an
        # optional goal (agent_run, Tk thread only; None while no planner runs).
        # Preflight's Ollama check runs on a worker thread and reports through
        # _agent_queue, drained by _poll_preview. A result whose job is no
        # longer current is ignored, and _agent_start_job (the job whose
        # result may start the agent) is cleared by every planner stop and F8.
        self.agent_run: AgentRun | None = None
        self.model_checker: Callable[[OllamaClientConfig], OllamaResult] = (
            lambda config: OllamaClient(config).check_model()
        )
        self._agent_queue: queue.SimpleQueue[tuple[int, OllamaClientConfig, bool, str]] = (
            queue.SimpleQueue()
        )
        self._preflight_job = 0
        self._agent_start_job: int | None = None
        self._preflight_thread: threading.Thread | None = None
        # Title of the captured window, for the preflight list.
        self._capture_title: str | None = None
        self.agent_status_var = tk.StringVar(value=AGENT_IDLE_TEXT)
        self.agent_checks_var = tk.StringVar(value=AGENT_NO_CHECKS_TEXT)
        self.agent_run_var = tk.StringVar(value=AGENT_NO_RUN_TEXT)
        self.planner_goal_var = tk.StringVar(value="")
        # Plain-str copy of the Goal field for the planner thread, which must
        # never read a Tk variable. Updated on the Tk thread by a trace.
        self._planner_goal = ""
        self.planner_goal_var.trace_add("write", self._sync_planner_goal)
        self.planner_mode_var = tk.StringVar(value="approve")
        self.planner_auto_steps_var = tk.StringVar(value=str(DEFAULT_AUTO_MAX_STEPS))
        self.planner_mode_status_var = tk.StringVar(value=PLANNER_APPROVE_MODE_TEXT)
        self.planner_proposal_var = tk.StringVar(value=PLANNER_NO_PROPOSAL_TEXT)

        # v0.8 session memory. Notes and session logs are read and written on
        # the Tk thread only. The planner thread reaches the notebook through
        # the controller's note sink (add_llm only) and reports each result
        # through _planner_queue. Nothing on the input path reads memory.
        self.memory_root = default_memory_root()
        self.notebook = NoteBook()
        self._notes_slug = check_slug(None)
        # None while notes.json is invalid: the notes are then read-only and
        # the file is never overwritten.
        self._notes_path: Path | None = None
        self._notes_error: str | None = None
        self._notes_saved_revision = 0
        # The last save failure, shown in the Memory status until a save works.
        self._notes_save_error: str | None = None
        # What the Memory list shows; edits check the note is still there.
        self._notes_shown: tuple[Note, ...] = ()
        self._notes_shown_revision = -1
        # One notebook per profile slug for the app's lifetime, so reloading a
        # profile keeps the planner's note rate limit.
        self._notebooks: dict[str, NoteBook] = {}
        # notes.json as last read or written per slug, to spot edits made
        # outside the app: those are re-read, never overwritten.
        self._notes_stamps: dict[str, tuple[int, int] | None] = {}
        self.session_log: SessionLogWriter | None = None
        self._session_truncated_reported = False
        self.memory_note_var = tk.StringVar(value="")
        self.memory_llm_notes_var = tk.BooleanVar(value=False)
        self.memory_status_var = tk.StringVar(value="")
        self.memory_session_var = tk.StringVar(value=MEMORY_NO_SESSION_TEXT)

        self.record_fps_var = tk.StringVar(value=f"{RECORDING_DEFAULT_FPS:g}")
        self.recording_status_var = tk.StringVar(value=RECORDING_IDLE_TEXT)
        # Tk-thread view of the recorder: "idle", "starting", "recording" or
        # "stopping". Status reports arrive from the recording thread through
        # a queue drained by _poll_preview (the recording thread never calls
        # into Tk, so it can never block on it), and are dropped unless they
        # belong to the current generation (bumped on every Record press).
        self._recording_ui_state = "idle"
        self._recording_generation = 0
        self._recording_status_queue: queue.SimpleQueue[tuple[int, RecordingStatus]] = (
            queue.SimpleQueue()
        )

        self._build_ui()
        self._switch_notebook(None)
        self.refresh_profiles()
        self._planner_logger = logging.getLogger("agent.planner_scheduler")
        self._planner_logger.setLevel(logging.INFO)
        self._planner_log_handler = _PlannerLogHandler(self)
        self._planner_logger.addHandler(self._planner_log_handler)
        self.refresh_windows()
        self._start_hotkey_listener()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_preview()

    # ---------------- UI ----------------

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")

        ttk.Label(top, text="Game window:").pack(side="left")
        self.combo = ttk.Combobox(
            top, textvariable=self.window_var, state="readonly", width=70
        )
        self.combo.pack(side="left", padx=8, fill="x", expand=True)
        ttk.Button(top, text="Refresh", command=self.refresh_windows).pack(side="left")

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 6))

        ttk.Button(controls, text="Start Capture", command=self.start_capture).pack(side="left")
        ttk.Button(controls, text="Stop Capture", command=self.stop_capture).pack(side="left", padx=5)
        ttk.Button(controls, text="Focus Game", command=self.focus_game).pack(side="left", padx=(8, 5))

        self.control_check = ttk.Checkbutton(
            controls,
            text="Enable keyboard/mouse control",
            variable=self.control_var,
            command=self._toggle_control,
        )
        self.control_check.pack(side="left", padx=(12, 5))

        ttk.Button(controls, text="Test W", command=self.test_w).pack(side="left", padx=3)
        ttk.Button(controls, text="Click Center", command=self.click_center).pack(side="left", padx=3)
        ttk.Button(
            controls, text="EMERGENCY STOP (F8)", command=self.emergency_stop
        ).pack(side="right")

        profile_box = ttk.LabelFrame(
            outer, text="Profile — detectors, skills and rules saved per game (load only with input control off)"
        )
        profile_box.pack(fill="x", pady=(4, 8))

        profile_row = ttk.Frame(profile_box)
        profile_row.pack(fill="x", padx=8, pady=(7, 4))

        ttk.Label(profile_row, text="Profile:").pack(side="left")
        self.profile_combo = ttk.Combobox(
            profile_row, textvariable=self.profile_var, state="readonly", width=32
        )
        self.profile_combo.pack(side="left", padx=(4, 8))
        ttk.Button(profile_row, text="Refresh", command=self.refresh_profiles).pack(side="left")
        ttk.Button(
            profile_row, text="Load Profile", command=self.load_selected_profile
        ).pack(side="left", padx=5)
        ttk.Button(
            profile_row, text="Save Profile…", command=self.save_current_profile
        ).pack(side="left")
        ttk.Label(
            profile_box, textvariable=self.profile_status_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        skills_box = ttk.LabelFrame(
            outer,
            text="Skills — from the loaded profile (off by default; run only with input control on)",
        )
        skills_box.pack(fill="x", pady=(0, 8))
        self.skills_frame = ttk.Frame(skills_box)
        self.skills_frame.pack(fill="x", padx=8, pady=(7, 0))
        ttk.Label(
            skills_box, textvariable=self.skills_status_var
        ).pack(anchor="w", padx=8, pady=(2, 7))

        vision_box = ttk.LabelFrame(outer, text="Vision v0.3 — Template + named detectors")
        vision_box.pack(fill="x", pady=(4, 8))

        vision_row = ttk.Frame(vision_box)
        vision_row.pack(fill="x", padx=8, pady=(7, 4))

        ttk.Button(
            vision_row,
            text="Select Template on Preview",
            command=self.enable_selection_mode,
        ).pack(side="left")

        ttk.Button(
            vision_row,
            text="Save Snapshot",
            command=self.save_snapshot,
        ).pack(side="left", padx=5)

        ttk.Button(
            vision_row,
            text="Clear Template",
            command=self.clear_template,
        ).pack(side="left", padx=5)

        ttk.Checkbutton(
            vision_row,
            text="Enable vision",
            variable=self.vision_enabled_var,
            command=self._toggle_vision,
        ).pack(side="left", padx=(15, 6))

        ttk.Label(vision_row, text="Threshold:").pack(side="left", padx=(10, 4))
        threshold = ttk.Spinbox(
            vision_row,
            from_=0.50,
            to=0.99,
            increment=0.01,
            textvariable=self.threshold_var,
            width=6,
            command=self._apply_threshold,
        )
        threshold.pack(side="left")
        threshold.bind("<Return>", lambda _e: self._apply_threshold())
        threshold.bind("<FocusOut>", lambda _e: self._apply_threshold())

        ttk.Label(vision_row, text="Detector name:").pack(side="left", padx=(15, 4))
        ttk.Entry(
            vision_row, textvariable=self.detector_name_var, width=16
        ).pack(side="left")
        ttk.Button(
            vision_row, text="Clear Detectors", command=self.clear_detectors
        ).pack(side="left", padx=5)

        ttk.Label(
            vision_box, textvariable=self.template_var
        ).pack(anchor="w", padx=8)
        ttk.Label(
            vision_box, textvariable=self.vision_var
        ).pack(anchor="w", padx=8)
        ttk.Label(
            vision_box, textvariable=self.detectors_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        rules_box = ttk.LabelFrame(
            outer, text="Rules — gated autonomous actions (only run when input control is enabled)"
        )
        rules_box.pack(fill="x", pady=(0, 8))

        rules_row = ttk.Frame(rules_box)
        rules_row.pack(fill="x", padx=8, pady=(7, 4))

        ttk.Label(rules_row, text="Rule name:").pack(side="left")
        ttk.Entry(rules_row, textvariable=self.rule_name_var, width=14).pack(
            side="left", padx=(4, 12)
        )

        ttk.Label(rules_row, text="Detector:").pack(side="left")
        ttk.Entry(rules_row, textvariable=self.rule_detector_var, width=14).pack(
            side="left", padx=(4, 12)
        )

        ttk.Label(rules_row, text="Min confidence:").pack(side="left")
        ttk.Spinbox(
            rules_row,
            from_=0.50,
            to=0.99,
            increment=0.01,
            textvariable=self.rule_min_confidence_var,
            width=6,
        ).pack(side="left", padx=(4, 12))

        ttk.Label(rules_row, text="Action: click").pack(side="left", padx=(0, 12))

        ttk.Button(rules_row, text="Add Rule", command=self.add_rule).pack(side="left")
        ttk.Button(
            rules_row, text="Clear Rules", command=self.clear_rules
        ).pack(side="left", padx=5)

        ttk.Label(
            rules_box, textvariable=self.rules_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        agent_box = ttk.LabelFrame(
            outer,
            text="Agent — a planner session with a time budget; it never turns on "
            "input control or auto mode",
        )
        agent_box.pack(fill="x", pady=(0, 8))

        agent_row = ttk.Frame(agent_box)
        agent_row.pack(fill="x", padx=8, pady=(7, 4))
        ttk.Button(
            agent_row, text="Preflight", command=self.run_agent_preflight
        ).pack(side="left")
        ttk.Button(
            agent_row, text="Start Agent", command=self.start_agent
        ).pack(side="left", padx=(4, 0))
        ttk.Button(
            agent_row, text="Stop Agent", command=self.stop_agent
        ).pack(side="left", padx=(4, 10))
        ttk.Label(agent_row, textvariable=self.agent_status_var).pack(side="left")
        ttk.Label(
            agent_box, textvariable=self.agent_checks_var, wraplength=1150, justify="left"
        ).pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Label(
            agent_box, textvariable=self.agent_run_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        planner_box = ttk.LabelFrame(outer, text="Planner (Ollama)")
        planner_box.pack(fill="x", pady=(0, 8))

        planner_row = ttk.Frame(planner_box)
        planner_row.pack(fill="x", padx=8, pady=(7, 4))

        ttk.Checkbutton(
            planner_row,
            text="Enable LLM planner",
            variable=self.planner_enabled_var,
            command=self._toggle_planner,
        ).pack(side="left")
        ttk.Label(planner_row, text="Model:").pack(side="left", padx=(15, 4))
        ttk.Entry(
            planner_row, textvariable=self.planner_model_var, width=20
        ).pack(side="left")
        ttk.Label(planner_row, text="Interval seconds:").pack(side="left", padx=(15, 4))
        ttk.Spinbox(
            planner_row,
            from_=1.0,
            to=600.0,
            increment=0.5,
            textvariable=self.planner_interval_var,
            width=7,
        ).pack(side="left")

        goal_row = ttk.Frame(planner_box)
        goal_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(goal_row, text="Goal:").pack(side="left")
        ttk.Entry(
            goal_row, textvariable=self.planner_goal_var, width=90
        ).pack(side="left", padx=(4, 0), fill="x", expand=True)

        mode_row = ttk.Frame(planner_box)
        mode_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Radiobutton(
            mode_row,
            text="Approve each step",
            value="approve",
            variable=self.planner_mode_var,
            command=self._set_planner_mode,
        ).pack(side="left")
        ttk.Radiobutton(
            mode_row,
            text="Auto",
            value="auto",
            variable=self.planner_mode_var,
            command=self._set_planner_mode,
        ).pack(side="left", padx=(10, 0))
        ttk.Label(mode_row, text="Auto max steps:").pack(side="left", padx=(15, 4))
        ttk.Spinbox(
            mode_row,
            from_=1,
            to=HARD_MAX_AUTO_STEPS,
            increment=1,
            textvariable=self.planner_auto_steps_var,
            width=5,
        ).pack(side="left")
        ttk.Label(
            mode_row, textvariable=self.planner_mode_status_var
        ).pack(side="left", padx=(15, 0))

        proposal_row = ttk.Frame(planner_box)
        proposal_row.pack(fill="x", padx=8, pady=(0, 4))
        self.planner_approve_button = ttk.Button(
            proposal_row, text="Approve", command=self.approve_planner_proposal
        )
        self.planner_approve_button.pack(side="left")
        self.planner_reject_button = ttk.Button(
            proposal_row, text="Reject", command=self.reject_planner_proposal
        )
        self.planner_reject_button.pack(side="left", padx=(4, 10))
        ttk.Label(
            proposal_row, textvariable=self.planner_proposal_var
        ).pack(side="left")
        self._refresh_planner_panel()

        ttk.Label(
            planner_box, textvariable=self.planner_status_var
        ).pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Label(
            planner_box, textvariable=self.planner_last_cycle_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        memory_box = ttk.LabelFrame(
            outer,
            text="Memory — notes shown to the planner as hints; they never change skills or keys",
        )
        memory_box.pack(fill="x", pady=(0, 8))

        notes_row = ttk.Frame(memory_box)
        notes_row.pack(fill="x", padx=8, pady=(7, 4))
        self.memory_listbox = tk.Listbox(
            notes_row, height=3, exportselection=False, activestyle="none"
        )
        self.memory_listbox.pack(side="left", fill="x", expand=True)
        notes_scroll = ttk.Scrollbar(
            notes_row, orient="vertical", command=self.memory_listbox.yview
        )
        notes_scroll.pack(side="left", fill="y")
        self.memory_listbox.configure(yscrollcommand=notes_scroll.set)
        self.memory_listbox.bind("<<ListboxSelect>>", self._memory_note_selected)

        note_row = ttk.Frame(memory_box)
        note_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(note_row, text="Note:").pack(side="left")
        ttk.Entry(
            note_row, textvariable=self.memory_note_var, width=70
        ).pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.memory_add_button = ttk.Button(
            note_row, text="Add", command=self.add_memory_note
        )
        self.memory_add_button.pack(side="left", padx=(8, 0))
        self.memory_edit_button = ttk.Button(
            note_row, text="Save Edit", command=self.edit_memory_note
        )
        self.memory_edit_button.pack(side="left", padx=(4, 0))
        self.memory_delete_button = ttk.Button(
            note_row, text="Delete", command=self.delete_memory_note
        )
        self.memory_delete_button.pack(side="left", padx=(4, 0))
        self.memory_llm_notes_check = ttk.Checkbutton(
            note_row,
            text="Let the planner write notes",
            variable=self.memory_llm_notes_var,
            command=self._toggle_llm_notes,
        )
        self.memory_llm_notes_check.pack(side="left", padx=(15, 0))

        ttk.Label(
            memory_box, textvariable=self.memory_status_var
        ).pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Label(
            memory_box, textvariable=self.memory_session_var
        ).pack(anchor="w", padx=8, pady=(0, 7))

        recording_box = ttk.LabelFrame(
            outer,
            text="Recording — your own demonstrations (off while input control is enabled)",
        )
        recording_box.pack(fill="x", pady=(0, 8))

        recording_row = ttk.Frame(recording_box)
        recording_row.pack(fill="x", padx=8, pady=(7, 4))

        self.record_button = ttk.Button(
            recording_row, text="Record", command=self.toggle_recording
        )
        self.record_button.pack(side="left")
        ttk.Label(recording_row, text="FPS:").pack(side="left", padx=(15, 4))
        self.record_fps_spin = ttk.Spinbox(
            recording_row,
            from_=RECORDING_MIN_FPS,
            to=RECORDING_MAX_FPS,
            increment=1,
            textvariable=self.record_fps_var,
            width=5,
        )
        self.record_fps_spin.pack(side="left")
        ttk.Label(
            recording_row,
            text="Frames, detector state and your keyboard/mouse input while the "
            "game window is focused. Saved locally in recordings/.",
        ).pack(side="left", padx=(15, 0))
        ttk.Label(
            recording_box, textvariable=self.recording_status_var
        ).pack(anchor="w", padx=8, pady=(0, 7))
        self._refresh_recording_controls()

        info = ttk.Frame(outer)
        info.pack(fill="x", pady=(0, 2))
        ttk.Label(info, textvariable=self.status_var).pack(side="left")
        ttk.Label(info, textvariable=self.fps_var).pack(side="right")

        ttk.Label(
            outer, textvariable=self.last_event_var
        ).pack(anchor="w", pady=(0, 6))

        preview_frame = ttk.LabelFrame(
            outer,
            text="Live game capture — drag a rectangle here after pressing Select Template"
        )
        preview_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(
            preview_frame,
            background="#111111",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)

        self.canvas.bind("<ButtonPress-1>", self._selection_press)
        self.canvas.bind("<B1-Motion>", self._selection_drag)
        self.canvas.bind("<ButtonRelease-1>", self._selection_release)

        log_frame = ttk.LabelFrame(outer, text="Safety / event log")
        log_frame.pack(fill="x", pady=(8, 0))
        self.logbox = tk.Text(log_frame, height=5, wrap="word", state="disabled")
        self.logbox.pack(fill="x", padx=6, pady=6)

        self.log("F8 global emergency stop armed.")
        self.log("Vision v0.3 ready. Input control starts DISABLED.")

    # ---------------- Logging ----------------

    def log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.last_event_var.set(f"Last event: {message}")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{stamp}] {message}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ---------------- Window / Capture ----------------

    def refresh_windows(self):
        previous_hwnd = None
        try:
            if self.windows and self.combo.current() >= 0:
                previous_hwnd = self.windows[self.combo.current()].hwnd
        except Exception:
            pass

        self.windows = list_visible_windows()
        labels = [w.label() for w in self.windows]
        self.combo["values"] = labels

        selected = -1
        if previous_hwnd is not None:
            for i, item in enumerate(self.windows):
                if item.hwnd == previous_hwnd:
                    selected = i
                    break

        if selected >= 0:
            self.combo.current(selected)
        elif labels:
            self.combo.current(0)

        self.log(f"Found {len(labels)} visible windows.")

    def selected_hwnd(self) -> int:
        idx = self.combo.current()
        if idx < 0 or idx >= len(self.windows):
            raise RuntimeError("Select a game window first.")
        return self.windows[idx].hwnd

    def start_capture(self):
        try:
            hwnd = self.selected_hwnd()
            self.stop_capture(silent=True)
            self.capture = WindowCapture(hwnd=hwnd, target_fps=30)
            self.capture.start()
            self._capture_title = win32gui.GetWindowText(hwnd)
            self.status_var.set("CAPTURING")
            self.log(f"Capture started: {self._capture_title}")
        except Exception as exc:
            messagebox.showerror("Start Capture", str(exc))
        finally:
            self._refresh_recording_controls()

    def stop_capture(self, silent: bool = False):
        # A recording samples this capture's frames; it cannot outlive it.
        if self._request_recording_stop(RECORDING_STOP_CAPTURE):
            self.log("Recording stopped because capture stopped.")
        if self.capture:
            self.capture.stop()
            self.capture = None
            self._capture_title = None
            self.latest_raw_frame = None
            self.latest_match = None
            self.fps_var.set("Capture: 0.0 FPS")
            if not silent:
                self.log("Capture stopped.")
        if self.status_var.get() != "EMERGENCY STOP (F8)":
            self.status_var.set("READY")
        self._refresh_recording_controls()

    def focus_game(self):
        try:
            hwnd = self.selected_hwnd()
            ok = focus_window(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if ok:
                self.log(f"Focused target window: {title}")
            else:
                self.log(f"Requested focus for target: {title}; Windows did not confirm foreground.")
            return ok
        except Exception as exc:
            self.log(f"Focus error: {exc}")
            messagebox.showerror("Focus Game", str(exc))
            return False

    # ---------------- Input ----------------

    def _toggle_control(self):
        if self.control_var.get():
            # Recording and autonomous input are mutually exclusive: the
            # dataset must only contain the player's own input.
            if self._request_recording_stop(RECORDING_STOP_INPUT_CONTROL):
                self.log("Recording stopped because input control was enabled.")
        self.input.set_enabled(self.control_var.get())
        if not self.control_var.get():
            # Input is already off, so a held key is released; end the skill too.
            self.executor.cancel()
            self._disarm_auto("input control was disabled")
            if self._drop_effect_watch("input control was disabled"):
                # The watched step is over; free the autopilot without counting it.
                self.autopilot.reset()
                self._refresh_planner_panel()
        if self.control_var.get():
            self.status_var.set("INPUT ENABLED")
            self.log("Keyboard/mouse control ENABLED.")
        else:
            if self.status_var.get() != "EMERGENCY STOP (F8)":
                self.status_var.set("CAPTURING" if self.capture else "READY")
            self.log("Keyboard/mouse control DISABLED; generated inputs released.")
        self._refresh_recording_controls()

    def test_w(self):
        if not self.control_var.get():
            messagebox.showwarning("Input disabled", "Enable keyboard/mouse control first.")
            return

        try:
            hwnd = self.selected_hwnd()
            title = win32gui.GetWindowText(hwnd)
            focus_window(hwnd, settle_seconds=0.15)
            self.log(f"Sending W for 0.25 seconds to: {title}")
            threading.Thread(target=self._safe_tap_w, daemon=True).start()
        except Exception as exc:
            self.log(f"Test W error: {exc}")
            messagebox.showerror("Test W", str(exc))

    def _safe_tap_w(self):
        try:
            self.input.tap_key("w", 0.25)
            self.root.after(0, lambda: self.log("Test W completed."))
        except Exception as exc:
            # `exc` is unbound by Python as soon as this except block exits, but
            # root.after runs the lambda later on the Tk main thread — capture the
            # message now so the deferred callback doesn't hit a NameError.
            message = str(exc)
            self.root.after(0, lambda: self.log(f"Input error: {message}"))

    def click_center(self):
        if not self.control_var.get():
            messagebox.showwarning("Input disabled", "Enable keyboard/mouse control first.")
            return
        try:
            hwnd = self.selected_hwnd()
            left, top, right, bottom = client_region(hwnd)
            x = (left + right) // 2
            y = (top + bottom) // 2
            self.input.click(x, y)
            self.log(f"Clicked target client center at screen ({x}, {y}).")
        except Exception as exc:
            self.log(f"Click error: {exc}")
            messagebox.showerror("Click", str(exc))

    def emergency_stop(self, recording_reason: str = RECORDING_STOP_F8):
        # Release input first; nothing should delay disabling input.
        self.input.set_enabled(False)
        # Then end the running skill (its held key is already released).
        self.executor.cancel()
        self.control_var.set(False)
        self.status_var.set("EMERGENCY STOP (F8)")
        planner_was_running = self.planner.is_running
        # Stopping the planner clears its mailbox; the pending proposal is
        # dropped and auto mode turns off.
        self.planner.stop()
        self._reset_planner_steps("of the emergency stop")
        self._reset_planner_last_cycle()
        # The log ends after everything that stops input, before recording.
        self._close_session_log(
            SESSION_END_APP_CLOSE if recording_reason == RECORDING_STOP_APP_CLOSE else SESSION_END_F8
        )
        self.planner_enabled_var.set(False)
        self.planner_status_var.set("Planner: disabled by emergency stop.")
        if planner_was_running:
            self.log("LLM planner stopped by emergency stop.")
        # Recording stops last; stop() is non-blocking.
        if self._request_recording_stop(recording_reason):
            if recording_reason == RECORDING_STOP_APP_CLOSE:
                self.log("Recording stopped because the app is closing.")
            else:
                self.log("Recording stopped by emergency stop.")
        self._refresh_recording_controls()
        self.log("EMERGENCY STOP: generated inputs released; input control disabled.")

    def _start_hotkey_listener(self):
        def on_press(key):
            if key == keyboard.Key.f8:
                # Stop input right here on the listener thread: the Tk thread
                # may be busy, and a running hold must not wait for it. Both
                # calls are thread-safe; emergency_stop then does the rest.
                self.input.set_enabled(False)
                self.executor.cancel()
                self.root.after(0, self.emergency_stop)

        self.hotkey_listener = keyboard.Listener(on_press=on_press)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

    # ---------------- Recording ----------------

    def toggle_recording(self):
        if self._recording_ui_state in ("starting", "recording"):
            if self._request_recording_stop(RECORDING_STOP_USER):
                self.log("Recording stop requested.")
            self._refresh_recording_controls()
            return
        self.start_recording()

    def start_recording(self):
        if self._recording_ui_state != "idle":
            return
        if self.capture is None:
            messagebox.showwarning("Record", "Start Capture first.")
            return
        if self.input.enabled or self.control_var.get():
            messagebox.showwarning(
                "Record", "Disable keyboard/mouse control before recording."
            )
            return
        try:
            fps = validate_fps(float(self.record_fps_var.get()))
        except ValueError:
            messagebox.showerror(
                "Record",
                f"Recording FPS must be a number from {RECORDING_MIN_FPS:g} "
                f"to {RECORDING_MAX_FPS:g}.",
            )
            return

        self._recording_generation += 1
        generation = self._recording_generation
        try:
            started = self.recorder.start(
                self.capture.hwnd,
                self.capture,
                self.game_state,
                self.recordings_dir,
                fps,
                on_status=lambda status: self._schedule_recording_status(generation, status),
            )
        except RecordingRefusedError as exc:
            messagebox.showwarning("Record", str(exc))
            return
        if not started:
            self.log("Previous recording is still closing; try Record again in a moment.")
            return

        self._recording_ui_state = "starting"
        self.recording_status_var.set(f"● REC starting at {fps:g} FPS…")
        self._refresh_recording_controls()
        self.log(
            f"Recording STARTED at {fps:g} FPS. Only your input while the game "
            "window is focused is recorded; F8 stops it."
        )

    def _request_recording_stop(self, reason: str) -> bool:
        """Ask the recorder to stop (non-blocking).

        Always forwards to the controller (a no-op when nothing runs). Returns
        True only when this call is what stopped an active recording.
        """
        self.recorder.stop(reason)
        if self._recording_ui_state not in ("starting", "recording"):
            return False
        self._recording_ui_state = "stopping"
        self.recording_status_var.set(f"Stopping recording ({reason})…")
        return True

    def _refresh_recording_controls(self):
        state = self._recording_ui_state
        if state in ("starting", "recording"):
            text, enabled = "Stop Recording", True
        elif state == "stopping":
            text, enabled = "Stopping…", False
        else:
            text = "Record"
            enabled = self.capture is not None and not self.input.enabled
        self.record_button.configure(text=text, state="normal" if enabled else "disabled")
        self.record_fps_spin.configure(state="normal" if state == "idle" else "disabled")

    def _schedule_recording_status(self, generation: int, status: RecordingStatus):
        """Called on the recording thread; hand the status to the Tk thread.

        Only a non-blocking queue put: unlike root.after, it cannot wait on a
        busy or closing Tk thread and stall the recorder's shutdown.
        """
        if not self._closing:
            self._recording_status_queue.put_nowait((generation, status))

    def _drain_recording_status(self):
        while True:
            try:
                generation, status = self._recording_status_queue.get_nowait()
            except queue.Empty:
                return
            self._show_recording_status(generation, status)

    def _show_recording_status(self, generation: int, status: RecordingStatus):
        if self._closing or generation != self._recording_generation:
            return
        if status.state == "recording":
            # A progress report queued before Stop must not undo "stopping".
            if self._recording_ui_state not in ("starting", "recording"):
                return
            self._recording_ui_state = "recording"
        elif status.state == "stopping":
            self._recording_ui_state = "stopping"
        elif status.state in ("stopped", "failed"):
            self._recording_ui_state = "idle"
            if status.state == "failed":
                self.log(f"Recording FAILED: {status.error}")
            else:
                name = status.session_dir.name if status.session_dir is not None else "?"
                self.log(
                    f"Recording saved to recordings/{name} "
                    f"({status.frames} frames, {status.dropped} dropped, "
                    f"{status.events} events; stop: {status.stop_reason})."
                )
        self.recording_status_var.set(format_recording_status(status))
        self._refresh_recording_controls()

    def _sync_recording_state(self):
        """Safety net if a final status report never reached the Tk thread.

        Runs after _drain_recording_status; the recorder queues its final
        report before is_running turns False, so this only fires if it was lost.
        """
        if self._recording_ui_state != "idle" and not self.recorder.is_running:
            self._recording_ui_state = "idle"
            self.recording_status_var.set("Recording: stopped (final status not received).")
            self._refresh_recording_controls()

    # ---------------- Vision ----------------

    def _apply_threshold(self):
        try:
            value = float(self.threshold_var.get())
            value = max(0.50, min(0.99, value))
            self.threshold_var.set(round(value, 2))
            self.matcher.threshold = value
            self.log(f"Vision threshold set to {value:.2f}.")
        except Exception:
            self.threshold_var.set(self.matcher.threshold)

    def _toggle_vision(self):
        if (
            self.vision_enabled_var.get()
            and not self.matcher.loaded
            and not self.registry.names
        ):
            self.vision_enabled_var.set(False)
            messagebox.showwarning(
                "No template",
                "Select a template on the preview or load a profile first."
            )
            return

        state = "ENABLED" if self.vision_enabled_var.get() else "DISABLED"
        self.log(f"Template vision {state}.")

    def enable_selection_mode(self):
        if self.latest_raw_frame is None:
            messagebox.showwarning(
                "No frame",
                "Start Capture and wait until the live preview is visible."
            )
            return
        self.selection_mode = True
        self.selection_start = None
        if self.selection_rect_id is not None:
            self.canvas.delete(self.selection_rect_id)
            self.selection_rect_id = None
        self.log("Template selection mode active. Drag a rectangle over the target UI/icon.")

    def clear_template(self):
        self.matcher.clear()
        self.latest_match = None
        self.vision_enabled_var.set(False)
        self.template_var.set("Template: none")
        self.vision_var.set("Vision: no template loaded")
        self.log("Template cleared.")

    def clear_detectors(self):
        self.registry.clear()
        self._detector_templates.clear()
        self._registry_visibility.clear()
        self.game_state.clear()
        self.detectors_var.set("Detectors: none registered")
        self.log("All named detectors cleared.")

    def add_rule(self):
        name = self.rule_name_var.get().strip()
        detector_name = self.rule_detector_var.get().strip()
        try:
            min_confidence = float(self.rule_min_confidence_var.get())
            rule = VisibilityRule(
                name=name,
                detector_name=detector_name,
                action="click",
                min_confidence=min_confidence,
            )
            self.rule_engine.add_rule(rule)
        except ValueError as exc:
            messagebox.showerror("Add Rule", str(exc))
            return

        self.rules_var.set(f"Rules: {len(self.rule_engine.rules)} active.")
        self.log(
            f"Rule added: '{name}' -> click on '{detector_name}' "
            f"(min confidence {min_confidence:.2f}). Still gated by input control."
        )

    def clear_rules(self):
        # The planner holds the old rule engine; stop it before replacing it.
        self._stop_planner_for("rules were cleared")
        self.rule_engine = RuleEngine()
        self.rules_var.set("Rules: 0 active.")
        self.log("All rules cleared.")

    def _stop_planner_for(self, why: str, *, session_end: str | None = None):
        planner_was_running = self.planner.is_running
        self.planner.stop()
        self._reset_planner_steps(why)
        self._reset_planner_last_cycle()
        self._close_session_log(session_end or why)
        self.planner_enabled_var.set(False)
        self.planner_status_var.set(f"Planner: disabled because {why}.")
        if planner_was_running:
            self.log(f"LLM planner stopped because {why}.")

    # ---------------- Profiles ----------------

    def _profile_permissions(self) -> SkillPermissions | None:
        """Called by the dispatcher, possibly on a worker thread (one attribute read)."""
        profile = self.profile
        return profile.permissions if profile is not None else None

    def refresh_profiles(self):
        try:
            names = list_profiles(self.profiles_dir)
        except OSError as exc:
            names = []
            self.log(f"Could not list profiles: {exc}")
        self.profile_combo["values"] = names
        if self.profile_var.get() not in names:
            self.profile_var.set(names[0] if names else "")

    def load_selected_profile(self):
        # Loading replaces the rules; with input off, none of them can fire
        # during the swap.
        if self.input.enabled or self.control_var.get():
            messagebox.showwarning(
                "Load Profile", "Disable keyboard/mouse control before loading a profile."
            )
            return
        if self.executor.busy:
            messagebox.showwarning(
                "Load Profile", "A skill is still stopping; try Load again in a moment."
            )
            return
        # Log finished runs under the profile they belong to.
        self._drain_skill_runs()
        folder_name = self.profile_var.get().strip()
        if not folder_name:
            messagebox.showwarning("Load Profile", "Choose a profile first.")
            return

        # Build everything before touching the app, so a bad profile changes nothing.
        try:
            profile = load_profile(self.profiles_dir / folder_name)
            templates = read_templates(profile)
            registry = DetectorRegistry()
            kept: dict[str, tuple[DetectorSpec, np.ndarray]] = {}
            for detector in profile.detectors:
                spec = detector.spec()
                registry.register_array(spec, templates[detector.name])
                kept[detector.name] = (spec, templates[detector.name])
            skill_book = profile.skill_book()
            rule_engine = profile.rule_engine()
            slug = check_slug(profile_slug(profile.name))
        except (ProfileError, ValueError, RuntimeError, cv2.error) as exc:
            self.log(f"Profile '{folder_name}' not loaded: {exc}")
            messagebox.showerror("Load Profile", str(exc))
            return

        self._stop_planner_for("a profile was loaded")
        self.registry = registry
        self._detector_templates = kept
        self._registry_visibility.clear()
        self.game_state.clear()
        self.rule_engine = rule_engine
        self.skill_book = skill_book
        self.profile = profile
        self._rebuild_skills_panel()
        # Planner steps of another profile's skills mean nothing here.
        self.step_history.clear()
        self.planner_goal_var.set(profile.planner.goal)
        if profile.planner.ollama is not None:
            self.planner_model_var.set(profile.planner.ollama.model)
        self.planner_auto_steps_var.set(str(profile.planner.auto_max_steps))
        self.memory_llm_notes_var.set(profile.planner.llm_notes)
        self._switch_notebook(slug)

        if profile.detectors:
            self.detectors_var.set(
                f"Detectors: {len(profile.detectors)} registered from profile."
            )
            self.vision_enabled_var.set(True)
        else:
            self.detectors_var.set("Detectors: none registered")
        self.rules_var.set(f"Rules: {len(rule_engine.rules)} active.")
        enabled = sum(skill_book.is_enabled(name) for name in skill_book.names)
        self.profile_status_var.set(
            f"Profile: {profile.name} (profiles/{folder_name}) · "
            f"{len(profile.detectors)} detectors · {len(skill_book.names)} skills "
            f"({enabled} enabled in file) · {len(rule_engine.rules)} rules"
        )
        self.log(
            f"Profile '{profile.name}' loaded from profiles/{folder_name}. "
            "Input control is still off."
        )

    def save_current_profile(self):
        contents = collect_profile_contents(
            self._detector_templates, self.rule_engine, self.profile
        )
        if not contents.detectors and not contents.skills:
            messagebox.showwarning(
                "Save Profile",
                "Nothing to save yet: register a detector on the preview or load a profile first.",
            )
            return

        name = simpledialog.askstring(
            "Save Profile",
            "Profile name:",
            initialvalue=self.profile.name if self.profile is not None else "",
            parent=self.root,
        )
        if name is None:
            return
        try:
            slug = profile_slug(name)
        except ProfileError as exc:
            messagebox.showerror("Save Profile", str(exc))
            return

        try:
            planner = self._planner_config_to_save()
        except ValueError as exc:
            messagebox.showerror("Save Profile", str(exc))
            return

        overwrite = False
        if (self.profiles_dir / slug).exists():
            if not messagebox.askyesno(
                "Save Profile",
                f"profiles/{slug} already exists. Replace its profile.json and "
                "template images? Other files in the folder are kept.",
            ):
                return
            overwrite = True

        try:
            save_profile(
                self.profiles_dir,
                name,
                detectors=contents.detectors,
                skills=contents.skills,
                rules=contents.rules,
                permissions=self.profile.permissions if self.profile is not None else None,
                planner=planner,
                expectations=self.profile.expectations if self.profile is not None else None,
                overwrite=overwrite,
            )
        except (ProfileError, ValueError) as exc:
            self.log(f"Profile not saved: {exc}")
            messagebox.showerror("Save Profile", str(exc))
            return

        self.refresh_profiles()
        self.profile_var.set(slug)
        self.log(
            f"Profile saved to profiles/{slug}: {len(contents.detectors)} detectors, "
            f"{len(contents.skills)} skills, {len(contents.rules)} rules. "
            "New click skills are saved disabled; edit profile.json to add key skills."
        )
        if contents.skipped_rules:
            self.log(
                "Rules not saved (their detector or skill is missing): "
                + ", ".join(contents.skipped_rules)
            )

    # ---------------- Skills ----------------

    def _rebuild_skills_panel(self):
        for child in self.skills_frame.winfo_children():
            child.destroy()
        self._skill_rows = {}
        book = self.skill_book
        if book is None or not book.names:
            self.skills_status_var.set(
                SKILLS_NONE_TEXT if book is None else "Skills: this profile has none."
            )
            return
        for index, name in enumerate(book.names):
            row, slot = divmod(index, SKILLS_PER_ROW)
            column = slot * 3
            enabled_var = tk.BooleanVar(value=book.is_enabled(name))
            result_var = tk.StringVar(value=SKILL_NO_RESULT_TEXT)
            ttk.Checkbutton(
                self.skills_frame,
                text=f"{name} ({describe_skill(book.get(name))})",
                variable=enabled_var,
                command=lambda n=name: self._toggle_skill(n),
            ).grid(row=row, column=column, sticky="w", pady=1)
            ttk.Button(
                self.skills_frame, text="Run", width=5, command=lambda n=name: self.run_skill(n)
            ).grid(row=row, column=column + 1, padx=4)
            ttk.Label(
                self.skills_frame, textvariable=result_var, width=36
            ).grid(row=row, column=column + 2, sticky="w", padx=(0, 12))
            self._skill_rows[name] = (enabled_var, result_var)
        self._update_skills_status()

    def _update_skills_status(self):
        book = self.skill_book
        if book is None:
            self.skills_status_var.set(SKILLS_NONE_TEXT)
            return
        enabled = sum(book.is_enabled(name) for name in book.names)
        self.skills_status_var.set(
            f"Skills: {len(book.names)} ({enabled} enabled). Key skills only run while "
            "the game window is in the foreground; Run focuses it first."
        )

    def _set_skill_result(self, name: str, text: str):
        row = self._skill_rows.get(name)
        if row is not None:
            row[1].set(text)

    def _toggle_skill(self, name: str):
        book = self.skill_book
        row = self._skill_rows.get(name)
        if book is None or row is None or book.get(name) is None:
            return
        enabled = bool(row[0].get())
        book.set_enabled(name, enabled)
        self._update_skills_status()
        self.log(f"Skill '{name}' {'ENABLED' if enabled else 'DISABLED'}.")
        # A disabled skill never runs: end it if it is running right now.
        if not enabled and self.executor.busy and self._running_skill == name:
            self.executor.cancel()
            self.log(f"Running skill '{name}' cancelled because it was disabled.")

    def _skill_blocked(self, name: str, source: str, reason: str):
        self._set_skill_result(name, f"BLOCKED: {reason}")
        self.log(f"Skill '{name}' ({source}) -> BLOCKED: {reason}")

    def run_skill(self, name: str):
        """Run button: focus the game window, then run one skill on the executor."""
        book = self.skill_book
        if book is None or book.get(name) is None:
            return
        if not self.input.enabled:
            messagebox.showwarning("Run Skill", "Enable keyboard/mouse control first.")
            return
        # Everything that can refuse is checked before focusing, so a refused
        # Run never moves the focus.
        if self.executor.busy:
            self._skill_blocked(name, "manual", "Busy: another skill is running.")
            return
        if isinstance(book.get(name), ClickSkill) and self.capture is None:
            # A click target comes from the captured frames of one window.
            self._skill_blocked(name, "manual", "Start Capture first; click skills aim at captured detections.")
            return
        built = book.build_intent(name, self.game_state, source="manual")
        if built.intent is None:
            self._skill_blocked(name, "manual", f"{built.reason}.")
            return
        try:
            # Same window as the rules: the one the frames come from.
            hwnd = self.capture.hwnd if self.capture else self.selected_hwnd()
            if not focus_window(hwnd, settle_seconds=0.15):
                self.log("Windows did not confirm the game window is in the foreground.")
        except Exception as exc:
            self._skill_blocked(name, "manual", str(exc))
            return

        # Rebuilt after focusing so the intent is fresh and the detection is
        # re-checked.
        built = book.build_intent(name, self.game_state, source="manual")
        if built.intent is None:
            self._skill_blocked(name, "manual", f"{built.reason}.")
            return
        refusal = self.executor.submit(built.intent, hwnd=hwnd, source="manual")
        if refusal is not None:
            self._skill_blocked(name, "manual", refusal)
            return
        self._running_skill = name
        self._set_skill_result(name, "running…")
        self.log(f"Skill '{name}' started (manual): {built.intent.reason}")

    def _submit_rule_skill(self, intent: ActionIntent, hwnd: int | None) -> tuple[str, str]:
        """Start the skill a fired skill rule names. Returns (outcome, reason)."""
        book = self.skill_book
        name = intent.skill_name or ""
        if book is None:
            return "blocked", "No profile loaded; skill rules cannot run."
        # Same first gate as the dispatcher; skips a worker thread per firing.
        if not self.input.enabled:
            return "blocked", "Input control is disabled."
        built = book.build_intent(name, self.game_state, source=intent.rule_name)
        if built.intent is None:
            return "blocked", f"{built.reason}."
        refusal = self.executor.submit(built.intent, hwnd=hwnd, source=f"rule {intent.rule_name}")
        if refusal is not None:
            return "blocked", refusal
        self._running_skill = name
        self._set_skill_result(name, "running…")
        return "submitted", f"skill '{name}' started."

    def _drain_skill_runs(self):
        for run in self.executor.drain():
            intent = run.result.intent
            name = intent.skill_name or intent.action
            outcome = "DONE" if run.result.dispatched else "BLOCKED"
            finished = datetime.fromtimestamp(run.finished_at).strftime("%H:%M:%S")
            self._set_skill_result(name, f"{finished} {outcome}: {run.result.reason}")
            self.log(f"Skill '{name}' ({run.source}) -> {outcome}: {run.result.reason}")
            # Identity, not source: a run from before a planner reset must
            # never be credited to a newer step.
            if intent is not None and intent is self._planner_intent:
                self._planner_intent = None
                self._planner_step_finished(
                    run.result.dispatched,
                    f"{outcome}: {run.result.reason}",
                    completed=not run.result.interrupted,
                )

    def _toggle_planner(self):
        if not self.planner_enabled_var.get():
            self.planner.stop()
            self._reset_planner_steps("the planner was disabled")
            self._reset_planner_last_cycle()
            self._close_session_log(SESSION_END_PLANNER_OFF)
            self.planner_status_var.set("Planner: disabled.")
            self.log("LLM planner DISABLED.")
            return

        try:
            interval_seconds = float(self.planner_interval_var.get())
            if not math.isfinite(interval_seconds) or interval_seconds < 1.0:
                raise ValueError("Planner interval must be a finite value of at least 1.0 seconds.")
            config = PlannerConfig(
                enabled=True,
                ollama=self._planner_client_config(),
                interval_seconds=interval_seconds,
            )
            self._reset_planner_steps("the planner was restarted")
            self._reset_planner_last_cycle()
            # Built before the planner starts, so a running planner always has a run.
            run = self._new_agent_run()
            generation = self._planner_generation
            book = self.skill_book
            # Read-only notes (an invalid notes.json) are never written to.
            allow_notes = bool(self.memory_llm_notes_var.get()) and self._notes_path is not None
            self.planner.start(
                self.rule_engine,
                config,
                on_cycle=lambda report: self._queue_planner_cycle_report(generation, report),
                # Only enabled skills are offered, re-read on every cycle.
                skills=SkillBookCatalog(book) if book is not None else None,
                history=self.step_history,
                goal=lambda: self._planner_goal,
                mailbox=self.proposals,
                should_plan=self._planner_may_plan,
                notes=self.notebook,
                allow_notes=allow_notes,
                on_note=lambda result: self._queue_planner_note(generation, result),
            )
            self.agent_run = run
        except ValueError as exc:
            self.planner_enabled_var.set(False)
            self.planner_status_var.set("Planner: disabled.")
            messagebox.showerror("Enable LLM planner", str(exc))
            return

        self.agent_status_var.set("Agent: running. Stop Agent or F8 ends the run.")
        self._refresh_agent_run()
        self.planner_status_var.set(
            f"Planner: enabled ({config.ollama.model}, every {config.interval_seconds:g}s)."
        )
        self._open_session_log(config.ollama.model, allow_notes)
        if book is None:
            scope = "No profile is loaded, so it can only enable or disable existing rules."
        else:
            scope = (
                "It can enable or disable rules and propose one enabled skill at a time; "
                "each proposal waits for Approve unless auto mode is on."
            )
        goal = f", or when {run.goal.describe()}" if run.goal is not None else ""
        self.log(
            f"LLM planner ENABLED ({config.ollama.host}:{config.ollama.port}). {scope} "
            f"Input control still gates every dispatch. "
            f"The run ends after {run.budget.max_seconds / 60:g} min{goal}."
        )

    def _planner_client_config(self) -> OllamaClientConfig:
        """Ollama settings: the Model field, with the profile's host, port and timeout."""
        model = self.planner_model_var.get().strip()
        profile = self.profile
        base = profile.planner.ollama if profile is not None else None
        if base is None:
            return OllamaClientConfig(model=model)
        return replace(base, model=model)

    def _new_agent_run(self) -> AgentRun:
        """The run a planner session is: the profile's time budget and goal."""
        planner = self.profile.planner if self.profile is not None else None
        minutes = planner.max_run_minutes if planner is not None else DEFAULT_MAX_RUN_MINUTES
        goal = planner.stop_when if planner is not None else None
        return AgentRun(RunBudget(minutes * 60.0, time.monotonic()), goal)

    def _reset_planner_last_cycle(self):
        """Invalidate pending cycle reports and clear the last-cycle label (Tk thread)."""
        self._planner_generation += 1
        self.planner_last_cycle_var.set(PLANNER_NO_CYCLE_TEXT)

    def _queue_planner_cycle_report(self, generation: int, report: PlannerCycleReport):
        """Called on the planner thread; queue the report for _poll_preview."""
        if not self._closing:
            self._planner_queue.put(("cycle", generation, report))

    def _queue_planner_note(self, generation: int, result: NoteResult):
        """Called on the planner thread; only queues the result for _poll_preview."""
        if not self._closing:
            self._planner_queue.put(("note", generation, result))

    def _drain_planner_queue(self):
        while True:
            try:
                kind, generation, payload = self._planner_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self.log(f"Planner: {payload}")
            elif kind == "note" and isinstance(payload, NoteResult):
                self._show_planner_note(generation, payload)
            elif isinstance(payload, PlannerCycleReport):
                self._show_planner_cycle_report(generation, payload)
                if generation == self._planner_generation and not self._closing:
                    self._session_write(
                        "cycle",
                        status=payload.status,
                        message=payload.message,
                        latency_s=round(payload.duration_seconds, 3),
                    )

    def _show_planner_note(self, generation: int, result: NoteResult):
        # The note is already in (or kept out of) the notebook; this only
        # reports it. _sync_notes saves it and refreshes the list.
        self.log(f"Planner {result.message}")
        if generation == self._planner_generation:
            self._session_write(
                "note",
                action="add" if result.stored else "skip",
                source="llm",
                text=result.text if result.stored else f"{result.text} ({result.reason})".strip(),
            )

    # ---------------- Agent runs (v1.0) ----------------

    def run_agent_preflight(self):
        """Preflight button: check everything a run needs; starts nothing."""
        self._begin_preflight(start=False)

    def start_agent(self):
        """Start Agent: run the preflight, then start the planner if it passes.

        This only starts a planner session (the run). Input control and auto
        mode stay exactly as the user set them.
        """
        if self.planner.is_running:
            self.log("The agent is already running.")
            return
        self._begin_preflight(start=True)

    def stop_agent(self):
        """Stop Agent: end the run (the planner session), or a pending start."""
        if self.planner.is_running:
            run = self.agent_run
            summary = f": {self._agent_run_summary(run)}" if run is not None else ""
            self._stop_planner_for("the agent was stopped", session_end=SESSION_END_AGENT_STOP)
            self.log(f"Agent stopped{summary}.")
        elif self._agent_start_job is not None:
            self._cancel_agent_start("you pressed Stop Agent")
        else:
            self.log("The agent is not running.")

    def _begin_preflight(self, *, start: bool):
        thread = self._preflight_thread
        if thread is not None and thread.is_alive():
            self.log("A preflight check is already running; try again when it finishes.")
            return
        self._preflight_job += 1
        job = self._preflight_job
        self._agent_start_job = job if start else None
        try:
            config = self._planner_client_config()
        except ValueError:
            # No network check without valid planner settings.
            self._finish_preflight(job, None, False, "no planner settings")
            return
        self.agent_status_var.set(
            "Agent: checking Ollama, then starting…" if start else "Agent: checking Ollama…"
        )
        thread = threading.Thread(
            target=self._check_model, args=(job, config), name="agent-preflight", daemon=True
        )
        self._preflight_thread = thread
        thread.start()

    def _check_model(self, job: int, config: OllamaClientConfig):
        """Preflight worker thread: the Ollama check only; the result is queued."""
        try:
            result = self.model_checker(config)
            if result.error is None:
                ok, detail = True, result.text or "available"
            else:
                ok, detail = False, result.error.message
        except Exception as exc:  # a failed check must never kill the thread silently
            ok, detail = False, f"check failed: {exc}"
        if not self._closing:
            self._agent_queue.put((job, config, ok, detail))

    def _drain_agent_queue(self):
        while True:
            try:
                job, config, ok, detail = self._agent_queue.get_nowait()
            except queue.Empty:
                return
            if job == self._preflight_job:
                self._finish_preflight(job, config, ok, detail)

    def _finish_preflight(
        self, job: int, config: OllamaClientConfig | None, ok: bool, detail: str
    ):
        """Show the report and, for Start Agent, start the planner if it passes.

        Everything except Ollama is read now, on the Tk thread, so a start
        uses facts that are still true after the (slow) Ollama check.
        """
        try:
            current: OllamaClientConfig | None = self._planner_client_config()
        except ValueError:
            current = None
        if config is not None and current != config:
            ok, detail = False, "the planner settings changed during the check; check again"
        report = run_preflight(self._preflight_facts(current, ok, detail))
        self._show_preflight(report)
        if self._agent_start_job != job:
            return
        self._agent_start_job = None
        if not report.ready:
            self.log(f"Agent not started: {report.summary()}")
            return
        if self.planner.is_running:
            self.log("The agent is already running.")
            return
        self.planner_enabled_var.set(True)
        self._toggle_planner()
        if not self.planner.is_running:
            self.log("Agent not started: the planner did not start.")
            return
        mode = "auto" if self.autopilot.mode == "auto" else "approve each step"
        input_state = "on" if self.input.enabled else "off (steps are refused until you turn it on)"
        self.log(f"Agent started. Mode: {mode}. Input control: {input_state}.")

    def _preflight_facts(
        self, config: OllamaClientConfig | None, ollama_ok: bool, ollama_detail: str
    ) -> PreflightFacts:
        book = self.skill_book
        enabled = tuple(name for name in book.names if book.is_enabled(name)) if book else ()
        return PreflightFacts(
            profile_name=self.profile.name if self.profile is not None else None,
            capture_running=self.capture is not None,
            window_title=self._capture_title if self.capture is not None else None,
            planner_configured=config is not None,
            model=config.model if config is not None else None,
            ollama_ok=ollama_ok,
            ollama_detail=ollama_detail,
            enabled_skills=enabled,
            input_enabled=self.input.enabled,
            goal=self.planner_goal_var.get(),
        )

    def _show_preflight(self, report: PreflightReport):
        self.agent_checks_var.set("   ".join(check.line() for check in report.checks))
        self.agent_status_var.set(f"Agent: {report.summary()}")

    def _cancel_agent_start(self, why: str):
        """A pending Start Agent never starts once anything stops the planner."""
        if self._agent_start_job is None:
            return
        self._agent_start_job = None
        self.agent_status_var.set(f"Agent: start cancelled because {why}.")
        self.log(f"Agent start cancelled because {why}.")

    def _poll_agent_run(self, now: float | None = None):
        """Tk loop: end the run when its budget is spent or its goal is seen."""
        run = self.agent_run
        if run is None:
            return
        now = time.monotonic() if now is None else now
        reason = run.stop_reason(self.game_state, now)
        if reason is None:
            self._refresh_agent_run(now)
            return
        self.log(f"Agent run ended because {reason}: {self._agent_run_summary(run)}.")
        self._stop_planner_for(reason)

    def _refresh_agent_run(self, now: float | None = None):
        run = self.agent_run
        if run is None:
            text = AGENT_NO_RUN_TEXT
        else:
            text = f"Run: {run.status_line(time.monotonic() if now is None else now)}"
        if self.agent_run_var.get() != text:
            self.agent_run_var.set(text)

    @staticmethod
    def _agent_run_summary(run: AgentRun) -> str:
        return (
            f"{run.steps} step(s), effects {run.effects['confirmed']} confirmed / "
            f"{run.effects['not_seen']} not seen"
        )

    # ---------------- Planner steps (v0.7) ----------------

    def _planner_may_plan(self) -> bool:
        """Scheduler-thread gate: skip the LLM call while a step is pending, a skill
        runs or a step's effect is still being watched.

        Reads only thread-safe state (mailbox, executor, an Event), never a Tk variable.
        """
        return (
            not self.proposals.occupied
            and not self.executor.busy
            and not self._effect_pending.is_set()
        )

    def _sync_planner_goal(self, *_args):
        """Tk trace on the Goal field: keep the plain-str copy the planner reads."""
        self._planner_goal = self.planner_goal_var.get().strip()[:MAX_GOAL_LENGTH]

    def _parse_auto_max_steps(self) -> int:
        try:
            value = int(self.planner_auto_steps_var.get().strip())
        except ValueError:
            raise ValueError(
                f"Auto max steps must be a whole number from 1 to {HARD_MAX_AUTO_STEPS}."
            ) from None
        return validate_auto_max_steps(value)

    def _planner_config_to_save(self) -> PlannerConfig:
        """The loaded profile's planner block with the current Goal and auto max steps."""
        base = self.profile.planner if self.profile is not None else PlannerConfig()
        return replace(
            base,
            goal=self.planner_goal_var.get().strip(),
            auto_max_steps=self._parse_auto_max_steps(),
            llm_notes=bool(self.memory_llm_notes_var.get()),
        )

    def _set_planner_mode(self):
        """Radio buttons: approve is always allowed; auto needs input, a planner and a yes."""
        if self.planner_mode_var.get() != "auto":
            self._disarm_auto("you chose approve mode")
            return
        if self.autopilot.mode == "auto":
            return

        def refuse(message: str):
            self.planner_mode_var.set("approve")
            messagebox.showwarning("Auto mode", message)

        if not self.input.enabled:
            refuse("Enable keyboard/mouse control first.")
            return
        if not self.planner.is_running:
            refuse("Enable the LLM planner first.")
            return
        if self.skill_book is None:
            refuse("Load a profile first; auto mode runs only its enabled skills.")
            return
        try:
            max_steps = self._parse_auto_max_steps()
        except ValueError as exc:
            refuse(str(exc))
            return
        target = self._planner_target_hwnd()
        if target is None:
            refuse("Select a game window first.")
            return
        generation, book = self.planner.generation, self.skill_book
        if not messagebox.askyesno(
            "Auto mode",
            f"The planner will run up to {max_steps} enabled skills without asking you. "
            f"Auto mode turns off after {max_steps} steps, after "
            f"{MAX_CONSECUTIVE_FAILURES} failed or blocked steps in a row, and on F8, "
            "input off, profile load, Clear Rules or planner off. Skills only run "
            "while the game window is in the foreground.\n\nTurn auto mode on?",
        ):
            self.planner_mode_var.set("approve")
            return
        # The dialog runs the Tk loop, so F8, input off, a planner restart, a
        # profile load or another window may have happened meanwhile.
        if (
            not self.input.enabled
            or not self.planner.is_running
            or self.planner.generation != generation
            or self.skill_book is not book
            or self._planner_target_hwnd() != target
        ):
            self.planner_mode_var.set("approve")
            self.log("Auto mode NOT turned on: something changed while the confirmation was open.")
            self._refresh_planner_panel()
            return
        self.autopilot.arm_auto(max_steps)
        self._auto_hwnd = target
        self._session_write("auto", on=True, reason="confirmed by the user", max_steps=max_steps)
        self.log(
            f"Auto mode ON: up to {max_steps} planner steps without approval. "
            "Press F8 to stop everything."
        )
        # One focus change caused by this confirmation, like Run; auto steps
        # themselves never move the focus.
        try:
            if not focus_window(target, settle_seconds=0.15):
                self.log("Windows did not confirm the game window is in the foreground.")
        except Exception as exc:
            self.log(f"Could not focus the game window: {exc}")
        self._refresh_planner_panel()

    def _planner_target_hwnd(self) -> int | None:
        """The window a planner step aims at: the captured one, else the selected one."""
        try:
            return self.capture.hwnd if self.capture else self.selected_hwnd()
        except Exception:
            return None

    def _disarm_auto(self, why: str):
        reason = self.autopilot.disarm(why)
        self._auto_hwnd = None
        self.planner_mode_var.set("approve")
        if reason is not None:
            self.log(f"Auto mode OFF because {reason}.")
            self._session_write("auto", on=False, reason=reason, max_steps=None)
        self._refresh_planner_panel()

    def _reset_planner_steps(self, why: str):
        """Planner stopped: auto off, a running planner skill cancelled, pending proposal
        and effect watch dropped, mailbox empty, the agent run over and a pending
        Start Agent cancelled."""
        self._disarm_auto(why)
        if self._planner_intent is not None and self.executor.busy:
            # The run is over, so its skill is too (a held key is released).
            self.executor.cancel()
            self.log(f"Planner skill cancelled because {why}.")
        dropped = self.autopilot.reset()
        self._drop_effect_watch(why)
        self.proposals.clear()
        self._planner_decision = None
        self._planner_intent = None
        if self.agent_run is not None:
            self.agent_run = None
            self.agent_status_var.set(f"Agent: run ended because {why}.")
        self._cancel_agent_start(why)
        if dropped is not None:
            self.log(f"Planner proposal '{dropped.skill_name}' dropped because {why}.")
        self._refresh_planner_panel()
        self._refresh_agent_run()

    def _poll_planner_proposals(self):
        """Tk loop: expire a stale pending proposal, then take and offer a new one."""
        now = time.monotonic()
        expired = self.autopilot.expire(now)
        if expired is not None:
            self.log(
                f"Planner proposal '{expired.skill_name}' expired "
                f"(not approved within {DEFAULT_TTL_SECONDS:g}s)."
            )
            self._finish_planner_step(expired, "expired", "not approved in time", None)

        proposal = self.proposals.take()
        if proposal is not None:
            self._offer_planner_proposal(proposal, now)
        self._refresh_planner_panel(now)

    def _offer_planner_proposal(self, proposal: SkillProposal, now: float):
        if not self.planner.is_running or proposal.generation != self.planner.generation:
            # From a stopped or replaced planner: never shown, never run.
            self.proposals.release()
            return
        if now - proposal.created_at > DEFAULT_TTL_SECONDS:
            # Posted long ago (the Tk loop was stalled): too old to show or run.
            self.log(f"Planner proposal '{proposal.skill_name}' expired before it was shown.")
            self._finish_planner_step(proposal, "expired", "too old when offered", None)
            return
        result = self.autopilot.offer(proposal, now)
        if result is OfferResult.DROPPED:
            self.proposals.release()
            self.log(f"Planner proposal '{proposal.skill_name}' dropped: a step is in progress.")
        elif result is OfferResult.EXECUTE:
            self._run_planner_step(proposal, "auto")
        else:
            self.log(
                f"Planner proposes '{proposal.skill_name}': {proposal.reason} "
                f"(Approve or Reject within {DEFAULT_TTL_SECONDS:g}s)."
            )

    def approve_planner_proposal(self):
        proposal = self.autopilot.approve(time.monotonic())
        if proposal is None:
            # Nothing pending, or it just expired (logged by the next poll).
            self._refresh_planner_panel()
            return
        self._run_planner_step(proposal, "approved")

    def reject_planner_proposal(self):
        proposal = self.autopilot.reject()
        if proposal is None:
            return
        self.log(f"Planner proposal '{proposal.skill_name}' rejected.")
        self._finish_planner_step(proposal, "rejected", "rejected by the user", None)

    def _run_planner_step(self, proposal: SkillProposal, decision: Decision):
        """Submit an approved or auto proposal like a Run: same checks, fresh intent.

        The skill must still be enabled in the loaded profile, and the
        dispatcher still applies every gate (input, allowlist, foreground,
        rate limit). Only an approval (a click in this app) focuses the game;
        auto mode never moves the focus.
        """
        name = proposal.skill_name
        self._planner_decision = decision
        refusal = self._planner_step_refusal(proposal)
        hwnd = None
        window_changed = False
        if refusal is None:
            try:
                hwnd = self.capture.hwnd if self.capture else self.selected_hwnd()
                if decision == "auto":
                    # Auto runs only in the window it was confirmed for, and
                    # never moves the focus: click skills need it too.
                    if hwnd != self._auto_hwnd:
                        window_changed = True
                        refusal = "The game window changed after auto mode was confirmed."
                    elif isinstance(self.skill_book.get(name), ClickSkill) and not (
                        is_foreground(hwnd)
                    ):
                        refusal = "Auto click skills need the game window in the foreground."
                elif not focus_window(hwnd, settle_seconds=0.15):
                    self.log("Windows did not confirm the game window is in the foreground.")
            except Exception as exc:
                refusal = str(exc)
        if refusal is None:
            # Rebuilt after focusing so the intent is fresh and re-checked.
            built = self.skill_book.build_intent(name, self.game_state, source=PLANNER_SOURCE)
            if built.intent is None:
                refusal = f"{built.reason}."
            else:
                refusal = self.executor.submit(built.intent, hwnd=hwnd, source=PLANNER_SOURCE)
        if refusal is not None:
            self._skill_blocked(name, PLANNER_SOURCE, refusal)
            self._planner_step_finished(False, f"refused: {refusal}", decision="refused")
            if window_changed:
                self._disarm_auto("the game window changed")
            return
        self._planner_intent = built.intent
        if self.agent_run is not None:
            self.agent_run.note_step()
        self._running_skill = name
        self._set_skill_result(name, "running…")
        self.log(f"Skill '{name}' started (planner, {decision}): {proposal.reason}")
        self._refresh_planner_panel()

    def _planner_step_refusal(self, proposal: SkillProposal) -> str | None:
        """Checks that need no focus change, in the same order as run_skill."""
        if not self.planner.is_running or proposal.generation != self.planner.generation:
            return "The planner is no longer running."
        name = proposal.skill_name
        book = self.skill_book
        if book is None or book.get(name) is None:
            return "The skill is not in the loaded profile."
        if not self.input.enabled:
            return "Input control is disabled."
        if self.executor.busy:
            return "Busy: another skill is running."
        if isinstance(book.get(name), ClickSkill) and self.capture is None:
            return "Start Capture first; click skills aim at captured detections."
        built = book.build_intent(name, self.game_state, source=PLANNER_SOURCE)
        if built.intent is None:
            return f"{built.reason}."
        return None

    def _planner_step_finished(
        self,
        ok: bool,
        outcome: str,
        *,
        decision: Decision | None = None,
        completed: bool = True,
    ):
        """Record the running planner step's result; auto may turn itself off."""
        proposal = self.autopilot.running
        if proposal is None:
            # The planner was stopped while the skill ran; nothing to record.
            return
        decision = decision or self._planner_decision or "approved"
        # Only a step that ran to completion, with input still on, gets an
        # effect watch (v1.0): an interrupted hold or a step drained after
        # input was turned off has nothing to confirm.
        watch = ok and completed and decision != "refused" and self.input.enabled
        expectation = self._step_expectation(proposal.skill_name) if watch else None
        if expectation is not None:
            # Set before the mailbox is released so the planner waits for the effect.
            self._effect_pending.set()
            try:
                record = self._finish_planner_step(
                    proposal, decision, outcome, ok,
                    effect=EFFECT_PENDING, expected=expectation.describe(),
                )
            except BaseException:
                self._effect_pending.clear()
                raise
            self._effect_watch = EffectWatch(proposal.skill_name, expectation, record.finished_at)
            self._effect_record = record
            # The autopilot counts the step when its effect resolves.
            self.log(
                f"Watching for {expectation.describe()} after '{proposal.skill_name}' "
                f"(up to {expectation.within_seconds:g}s)."
            )
            self._refresh_planner_panel()
            return
        self._finish_planner_step(proposal, decision, outcome, ok if decision != "refused" else None)
        self._record_autopilot_result(ok)

    def _record_autopilot_result(self, ok: bool):
        auto_off = self.autopilot.record_result(ok)
        if auto_off is not None:
            self._auto_hwnd = None
            self.planner_mode_var.set("approve")
            self._session_write("auto", on=False, reason=auto_off, max_steps=None)
            self.log(f"Auto mode OFF: {auto_off}.")
            self._refresh_planner_panel()

    def _step_expectation(self, skill_name: str) -> Expectation | None:
        profile = self.profile
        return profile.expectations.get(skill_name) if profile is not None else None

    def _poll_effect_watch(self, now: float | None = None):
        """Tk loop: resolve the pending effect watch against the latest GameState."""
        watch = self._effect_watch
        if watch is None:
            return
        now = time.monotonic() if now is None else now
        effect = watch.check(self.game_state, now)
        result = watch.result
        if effect == EFFECT_PENDING or result is None:
            return
        record = self._effect_record
        self._effect_watch = None
        self._effect_record = None
        if record is not None:
            self.step_history.set_effect(record, effect)
        if self.agent_run is not None:
            self.agent_run.note_effect(effect)
        self._session_write(
            "effect",
            skill=result.skill_name,
            effect=effect,
            detector=result.detector,
            waited_s=result.waited_s,
        )
        seen = "not seen" if effect == EFFECT_NOT_SEEN else effect
        self.log(
            f"Effect of '{result.skill_name}': {seen} "
            f"({watch.expectation.describe()}, {result.waited_s:.1f}s)."
        )
        # A step whose effect was not seen counts as failed; it is never retried.
        self._record_autopilot_result(effect != EFFECT_NOT_SEEN)
        self._effect_pending.clear()
        self._refresh_planner_panel(now)

    def _drop_effect_watch(self, why: str) -> bool:
        """Forget the pending effect watch without recording an effect."""
        watch, record = self._effect_watch, self._effect_record
        self._effect_watch = None
        self._effect_record = None
        self._effect_pending.clear()
        if watch is None:
            return False
        if record is not None:
            self.step_history.set_effect(record, "none")
        self.log(f"Effect watch for '{watch.skill_name}' dropped because {why}.")
        return True

    def _finish_planner_step(
        self,
        proposal: SkillProposal,
        decision: Decision,
        outcome: str,
        ok: bool | None,
        *,
        effect: str = "none",
        expected: str = "",
    ) -> StepRecord:
        """Feed the step back to the planner and free the mailbox for the next one."""
        record = StepRecord(
            proposal.skill_name, proposal.reason, decision, outcome, ok, time.monotonic(),
            effect=effect, expected=expected,
        )
        self.step_history.append(record)
        self._planner_decision = None
        self.proposals.release()
        self._session_write(
            "step",
            skill=proposal.skill_name,
            reason=proposal.reason,
            decision=decision,
            outcome=outcome,
            ok=ok,
        )
        self._refresh_planner_panel()
        return record

    def _refresh_planner_panel(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        autopilot = self.autopilot
        if autopilot.mode == "auto":
            mode_text = f"Mode: AUTO · {autopilot.steps_taken}/{autopilot.max_steps} steps."
        else:
            mode_text = PLANNER_APPROVE_MODE_TEXT
        pending, running = autopilot.pending, autopilot.running
        if pending is not None:
            proposal_text = (
                f"Proposal: {pending.skill_name} — {self._short(pending.reason)} · "
                f"{math.ceil(autopilot.seconds_left(now))}s left"
            )
        elif self._effect_watch is not None:
            watch = self._effect_watch
            proposal_text = (
                f"Watching: {watch.skill_name} — expecting {watch.expectation.describe()} · "
                f"{math.ceil(max(0.0, watch.deadline - now))}s left"
            )
        elif running is not None:
            proposal_text = f"Running: {running.skill_name} — {self._short(running.reason)}"
        else:
            proposal_text = PLANNER_NO_PROPOSAL_TEXT
        if self.planner_mode_status_var.get() != mode_text:
            self.planner_mode_status_var.set(mode_text)
        if self.planner_proposal_var.get() != proposal_text:
            self.planner_proposal_var.set(proposal_text)
        state = ["!disabled"] if pending is not None else ["disabled"]
        for button in (
            getattr(self, "planner_approve_button", None),
            getattr(self, "planner_reject_button", None),
        ):
            if button is not None:
                button.state(state)

    @staticmethod
    def _short(text: str) -> str:
        text = " ".join(text.split())
        if len(text) > PLANNER_MESSAGE_MAX_CHARS:
            return text[: PLANNER_MESSAGE_MAX_CHARS - 1] + "…"
        return text

    def _show_planner_cycle_report(self, generation: int, report: PlannerCycleReport):
        if self._closing or generation != self._planner_generation:
            return

        message = " ".join(report.message.split())
        if len(message) > PLANNER_MESSAGE_MAX_CHARS:
            message = message[: PLANNER_MESSAGE_MAX_CHARS - 1] + "…"
        finished = datetime.fromtimestamp(report.finished_at).strftime("%H:%M:%S")
        if report.duration_seconds < 1.0:
            duration = f"{report.duration_seconds * 1000:.0f}ms"
        else:
            duration = f"{report.duration_seconds:.1f}s"
        self.planner_last_cycle_var.set(
            f"Last cycle: {finished} · {duration} · {report.status} · {message}"
        )

    # ---------------- Memory (v0.8) ----------------

    def _switch_notebook(self, slug: str | None):
        """Show the notes of profile ``slug`` (None: no profile). Tk thread only; never raises."""
        self._save_notes_if_changed()
        key = check_slug(slug)
        path = notes_path(self.memory_root, key)
        cached = self._notebooks.get(key)
        stamp = _file_stamp(path)
        error = None
        if cached is not None and stamp == self._notes_stamps.get(key):
            book = cached
        else:
            try:
                book = load_notes(path)
            except Exception as exc:  # a broken file must never stop the app
                book, error = NoteBook(), str(exc)
                self._notebooks.pop(key, None)
            else:
                if cached is not None:
                    self.log(f"memory/{key}/notes.json changed outside the app; reloaded it.")
                self._notebooks[key] = book
                self._notes_stamps[key] = stamp
            if cached is not None:
                # The file wins, but the planner's rate limit carries over.
                book.inherit_rate_limit(cached)
        self.notebook = book
        self._notes_slug = key
        self._notes_path = None if error else path
        self._notes_error = error
        self._notes_save_error = None
        self._notes_saved_revision = book.revision
        if error:
            self.log(f"Notes are read-only until notes.json is fixed or moved: {error}")
        self._refresh_memory_panel(force=True)

    def _save_notes_if_changed(self):
        path = self._notes_path
        if path is None:
            return
        revision = self.notebook.revision
        if revision == self._notes_saved_revision:
            return
        # One attempt (and at most one report) per change, never a retry loop.
        self._notes_saved_revision = revision
        if _file_stamp(path) != self._notes_stamps.get(self._notes_slug):
            # Edited or broken by hand while the app ran: never overwrite it.
            self._notes_path = None
            self._notes_error = "notes.json changed outside the app"
            self.log(
                f"Notes not saved: memory/{self._notes_slug}/notes.json changed outside the app. "
                "Load the profile again to use the file; the change made here is dropped."
            )
            self._refresh_memory_panel(force=True)
            return
        try:
            save_notes(path, self.notebook)
        except NotesError as exc:
            self._notes_save_error = str(exc)
            self.log(f"Notes not saved: {exc}")
        else:
            self._notes_save_error = None
            self._notes_stamps[self._notes_slug] = _file_stamp(path)
        self._refresh_memory_panel(force=True)

    def _sync_notes(self):
        """Tk loop: save notes the planner added and show them."""
        self._save_notes_if_changed()
        self._refresh_memory_panel()

    def _refresh_memory_panel(self, *, force: bool = False):
        revision = self.notebook.revision
        if not force and revision == self._notes_shown_revision:
            return
        selected = self._selected_note()
        notes = self.notebook.notes()
        self._notes_shown_revision = revision
        self._notes_shown = notes
        self.memory_listbox.delete(0, "end")
        for note in notes:
            self.memory_listbox.insert("end", f"[{note.source}] {note.text}")
        if selected is not None and selected[1] in notes:
            # A planner note may shift the list; keep the user's note selected.
            self.memory_listbox.selection_set(notes.index(selected[1]))
        where = f"memory/{self._notes_slug}/notes.json"
        if self._notes_error is not None:
            status = f"Notes: READ-ONLY — {where}: fix or move it, then load the profile (see the log)."
        else:
            from_llm = sum(note.source == "llm" for note in notes)
            status = (
                f"Notes: {len(notes)}/{MAX_NOTES} ({from_llm}/{MAX_LLM_NOTES} from the planner) · "
                f"{where}"
            )
            if self._notes_save_error is not None:
                status += " · NOT SAVED (see the log)"
        self.memory_status_var.set(status)
        read_only = self._notes_error is not None
        for widget in (self.memory_add_button, self.memory_edit_button, self.memory_delete_button):
            widget.state(["disabled"] if read_only else ["!disabled"])
        # Planner notes follow a profile's planner.llm_notes: no profile, no planner notes.
        if self.profile is None:
            self.memory_llm_notes_var.set(False)
        no_llm_notes = read_only or self.profile is None
        self.memory_llm_notes_check.state(["disabled"] if no_llm_notes else ["!disabled"])

    def _memory_note_selected(self, _event=None):
        selected = self._selected_note()
        if selected is not None:
            self.memory_note_var.set(selected[1].text)

    def _selected_note(self) -> tuple[int, Note] | None:
        selection = self.memory_listbox.curselection()
        if not selection or selection[0] >= len(self._notes_shown):
            return None
        return selection[0], self._notes_shown[selection[0]]

    def add_memory_note(self):
        if self._notes_path is None:
            return
        try:
            note = self.notebook.add_user(self.memory_note_var.get())
        except NotesError as exc:
            messagebox.showwarning("Memory", str(exc))
            return
        self._user_note_changed("add", note.text)

    def edit_memory_note(self):
        selected = self._selected_note()
        if self._notes_path is None:
            messagebox.showwarning("Memory", "The notes are read-only (see the Memory status).")
            return
        if selected is None:
            messagebox.showwarning("Memory", "Select a note to edit first.")
            return
        index, shown = selected
        try:
            note = self.notebook.edit(index, self.memory_note_var.get(), expected=shown)
        except NotesError as exc:
            messagebox.showwarning("Memory", str(exc))
            self._refresh_memory_panel()
            return
        self._user_note_changed("edit", note.text)

    def delete_memory_note(self):
        selected = self._selected_note()
        if self._notes_path is None:
            messagebox.showwarning("Memory", "The notes are read-only (see the Memory status).")
            return
        if selected is None:
            messagebox.showwarning("Memory", "Select a note to delete first.")
            return
        index, shown = selected
        try:
            note = self.notebook.delete(index, expected=shown)
        except NotesError as exc:
            messagebox.showwarning("Memory", str(exc))
            self._refresh_memory_panel()
            return
        self._user_note_changed("delete", note.text)

    def _user_note_changed(self, action: str, text: str):
        self.memory_note_var.set("")
        self._save_notes_if_changed()
        self._refresh_memory_panel()
        self._session_write("note", action=action, source="user", text=text)
        past = {"add": "added", "edit": "edited", "delete": "deleted"}[action]
        self.log(f"Note {past}: {text}")

    def _toggle_llm_notes(self):
        state = "ON" if self.memory_llm_notes_var.get() else "OFF"
        when = " (from the next planner start)" if self.planner.is_running else ""
        self.log(
            f"Planner notes {state}{when}. Save Profile keeps the choice. "
            "Notes are hints only; they never change skills or keys."
        )

    def _memory_display(self, path: Path) -> str:
        try:
            return path.relative_to(self.memory_root).as_posix()
        except ValueError:
            return str(path)

    def _open_session_log(self, model: str, llm_notes: bool):
        """Start this planner session's log file; a failure only turns logging off."""
        self._close_session_log(SESSION_END_RESTART)
        try:
            path = new_session_path(self.memory_root, self._notes_slug)
        except (OSError, ValueError) as exc:
            self.memory_session_var.set("Session log: off (could not create the file; see the log).")
            self.log(f"Session log off: could not create a session file: {exc}")
            return
        self.session_log = SessionLogWriter(path)
        self._session_truncated_reported = False
        self.memory_session_var.set(f"Session log: {self._memory_display(path)}")
        try:
            auto_max_steps = self._parse_auto_max_steps()
        except ValueError:
            auto_max_steps = DEFAULT_AUTO_MAX_STEPS
        self._session_write(
            "session_start",
            app_version=APP_VERSION,
            profile=self.profile.name if self.profile is not None else None,
            model=model,
            goal=self._planner_goal,
            auto_max_steps=auto_max_steps,
            llm_notes=llm_notes,
        )

    def _session_write(self, event_type: str, **fields: object):
        """Append one record to the session log. Never raises; a failure turns it off."""
        log = self.session_log
        if log is None:
            return
        try:
            log.write(event_type, **fields)
        except Exception as exc:  # logging must never break the caller
            error: str | None = f"invalid {event_type} record: {exc}"
        else:
            error = log.error
        if error is not None:
            self.session_log = None
            self.memory_session_var.set("Session log: OFF after an error (see the log).")
            self.log(f"Session log turned off for the rest of this session: {error}")
        elif log.truncated and not self._session_truncated_reported:
            self._session_truncated_reported = True
            self.memory_session_var.set(
                f"Session log: full; the rest of this session is not logged · "
                f"{self._memory_display(log.path)}"
            )
            self.log("Session log is full; the rest of this session is not logged.")

    def _close_session_log(self, reason: str):
        log = self.session_log
        if log is None:
            return
        self.session_log = None
        try:
            log.close(reason)
        except Exception as exc:  # logging must never break the caller
            self.log(f"Session log error: {exc}")
        if log.failed:
            self.log(f"Session log error: {log.error}")
        self.memory_session_var.set(
            f"Session log: ended ({reason}) · {self._memory_display(log.path)}"
        )

    def save_snapshot(self):
        frame = self.latest_raw_frame
        if frame is None:
            messagebox.showwarning("No frame", "Start Capture first.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.snapshots_dir / f"snapshot_{stamp}.png"
        ok = cv2.imwrite(str(path), frame)
        if ok:
            self.log(f"Snapshot saved: {path.name}")
        else:
            messagebox.showerror("Snapshot", "Could not save snapshot.")

    def _selection_press(self, event):
        if not self.selection_mode:
            return
        self.selection_start = (event.x, event.y)
        if self.selection_rect_id is not None:
            self.canvas.delete(self.selection_rect_id)
        self.selection_rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00ff66",
            width=2
        )

    def _selection_drag(self, event):
        if not self.selection_mode or self.selection_start is None:
            return
        x0, y0 = self.selection_start
        self.canvas.coords(self.selection_rect_id, x0, y0, event.x, event.y)

    def _selection_release(self, event):
        if not self.selection_mode or self.selection_start is None:
            return

        x0, y0 = self.selection_start
        x1, y1 = event.x, event.y

        self.selection_mode = False
        self.selection_start = None

        left = min(x0, x1)
        top = min(y0, y1)
        right = max(x0, x1)
        bottom = max(y0, y1)

        try:
            sx0, sy0 = self._canvas_to_source(left, top)
            sx1, sy1 = self._canvas_to_source(right, bottom)
        except Exception as exc:
            self.log(f"Selection error: {exc}")
            return

        sx0 = max(0, min(self.preview_source_w - 1, sx0))
        sy0 = max(0, min(self.preview_source_h - 1, sy0))
        sx1 = max(0, min(self.preview_source_w, sx1))
        sy1 = max(0, min(self.preview_source_h, sy1))

        if sx1 - sx0 < 8 or sy1 - sy0 < 8:
            self.log("Selection rejected: template is too small.")
            return

        frame = self.latest_raw_frame
        if frame is None:
            self.log("Selection rejected: no current frame.")
            return

        roi = frame[sy0:sy1, sx0:sx1].copy()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.templates_dir / f"template_{stamp}.png"

        if not cv2.imwrite(str(path), roi):
            self.log("Could not save selected template.")
            return

        self.matcher.load_array(roi, path)
        self.template_var.set(
            f"Template: {path.name}  |  {roi.shape[1]}x{roi.shape[0]} px"
        )
        self.vision_enabled_var.set(True)
        self.log(
            f"Template created: {path.name}. Vision automatically enabled."
        )

        detector_name = self.detector_name_var.get().strip()
        if detector_name:
            try:
                self.registry.unregister(detector_name)
                self._detector_templates.pop(detector_name, None)
                self._registry_visibility.pop(detector_name, None)
                spec = DetectorSpec(name=detector_name, threshold=self.threshold_var.get())
                self.registry.register_array(spec, roi)
                self._detector_templates[detector_name] = (spec, roi)
                self.log(
                    f"Detector '{detector_name}' registered "
                    f"({len(self.registry.names)} total)."
                )
            except ValueError as exc:
                self.log(f"Detector registration error: {exc}")
        else:
            self.log("No detector name set; skipping multi-detector registration.")

    def _canvas_to_source(self, cx: int, cy: int) -> tuple[int, int]:
        if self.preview_scale <= 0:
            raise RuntimeError("Preview transform is not ready.")

        x = (cx - self.preview_offset_x) / self.preview_scale
        y = (cy - self.preview_offset_y) / self.preview_scale

        return int(round(x)), int(round(y))

    def _run_vision_if_due(self, frame):
        if not self.vision_enabled_var.get():
            self.latest_match = None
            if self.matcher.loaded:
                self.vision_var.set("Vision: template loaded, detector disabled")
            return

        now = time.perf_counter()
        if now - self._last_vision_time < self._vision_interval:
            return

        self._last_vision_time = now

        if self.matcher.loaded:
            try:
                match = self.matcher.find_best(frame)
                self.latest_match = match

                if match is None:
                    self.vision_var.set(
                        f"Vision: NOT FOUND  | threshold {self.matcher.threshold:.2f}"
                    )
                else:
                    self.vision_var.set(
                        f"Vision: FOUND  | score {match.score:.3f}  | "
                        f"x={match.x}, y={match.y}, w={match.w}, h={match.h}"
                    )
            except Exception as exc:
                self.latest_match = None
                self.vision_var.set(f"Vision error: {exc}")

        if self.registry.names:
            try:
                detections = self.registry.detect_all(frame)
            except ValueError as exc:
                self.detectors_var.set(f"Detectors error: {exc}")
                return

            apply_detections(self.game_state, detections.values())

            parts = []
            for name in sorted(detections):
                detection = detections[name]
                was_visible = self._registry_visibility.get(name)
                if was_visible != detection.visible:
                    self._registry_visibility[name] = detection.visible
                    state_txt = "FOUND" if detection.visible else "LOST"
                    self.log(
                        f"Detector '{name}': {state_txt} "
                        f"(confidence {detection.confidence:.2f})."
                    )
                status = "FOUND" if detection.visible else "not found"
                parts.append(f"{name}={status}({detection.confidence:.2f})")

            self.detectors_var.set("Detectors: " + "  |  ".join(parts))

        if self.rule_engine.rules:
            intents = self.rule_engine.evaluate(self.game_state)
            if intents:
                # Use the window this frame actually came from, not whatever
                # the window-picker combobox currently shows -- the two can
                # diverge if the user reselects the combobox while capture
                # keeps running against the original window, and a bbox from
                # one window's frame is meaningless on another window's
                # screen coordinates.
                hwnd = self.capture.hwnd if self.capture else None

                last_outcome = "blocked"
                for intent in intents:
                    if intent.action == SKILL_RULE_ACTION:
                        # Profile rules name a skill; it runs on the executor
                        # and its result is logged when drained.
                        last_outcome, reason = self._submit_rule_skill(intent, hwnd)
                    else:
                        # UI click rules are quick, so they still dispatch here;
                        # the dispatcher refuses them while a skill holds a key.
                        result = self.dispatcher.dispatch(intent, hwnd=hwnd)
                        last_outcome = "dispatched" if result.dispatched else "blocked"
                        reason = result.reason
                    self.log(
                        f"Rule '{intent.rule_name}' target={intent.detector_name} "
                        f"confidence={intent.confidence:.2f} -> "
                        f"{last_outcome.upper()}: {reason}"
                    )

                self.rules_var.set(
                    f"Rules: {len(self.rule_engine.rules)} active. "
                    f"Last: '{intents[-1].rule_name}' {last_outcome}."
                )

    # ---------------- Preview ----------------

    def _poll_preview(self):
        if self._closing:
            return

        self._drain_recording_status()
        self._sync_recording_state()
        self._drain_skill_runs()
        self._drain_planner_queue()
        self._drain_agent_queue()
        self._sync_notes()
        self._poll_planner_proposals()

        if self.capture:
            frame = self.capture.latest_frame()
            self.fps_var.set(f"Capture: {self.capture.actual_fps:.1f} FPS")

            if self.capture.last_error:
                self.status_var.set(f"Capture error: {self.capture.last_error}")

            if frame is not None:
                self.latest_raw_frame = frame
                self._run_vision_if_due(frame)
                self._draw_preview(frame)

        # After vision, so this frame's observations count.
        self._poll_effect_watch()
        self._poll_agent_run()
        self.root.after(33, self._poll_preview)

    def _draw_preview(self, frame):
        source_h, source_w = frame.shape[:2]
        self.preview_source_w = source_w
        self.preview_source_h = source_h

        canvas_w = max(100, self.canvas.winfo_width())
        canvas_h = max(100, self.canvas.winfo_height())

        scale = min(
            canvas_w / source_w,
            canvas_h / source_h,
            1.0
        )
        disp_w = max(1, int(source_w * scale))
        disp_h = max(1, int(source_h * scale))

        if disp_w != source_w or disp_h != source_h:
            display = cv2.resize(
                frame,
                (disp_w, disp_h),
                interpolation=cv2.INTER_AREA
            )
        else:
            display = frame

        # Draw detected box on preview copy.
        if self.latest_match is not None:
            m = self.latest_match
            x1 = int(m.x * scale)
            y1 = int(m.y * scale)
            x2 = int((m.x + m.w) * scale)
            y2 = int((m.y + m.h) * scale)
            cv2.rectangle(display, (x1, y1), (x2, y2), (0, 255, 80), 2)

        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        self.preview_photo = ImageTk.PhotoImage(image=image)

        xoff = (canvas_w - disp_w) // 2
        yoff = (canvas_h - disp_h) // 2

        self.preview_scale = scale
        self.preview_offset_x = xoff
        self.preview_offset_y = yoff

        if self.preview_image_item is None:
            self.preview_image_item = self.canvas.create_image(
                xoff, yoff,
                image=self.preview_photo,
                anchor="nw"
            )
        else:
            self.canvas.coords(self.preview_image_item, xoff, yoff)
            self.canvas.itemconfigure(
                self.preview_image_item,
                image=self.preview_photo
            )

        # Keep the user's active selection rectangle on top.
        if self.selection_rect_id is not None:
            self.canvas.tag_raise(self.selection_rect_id)

    # ---------------- Shutdown ----------------

    def close(self):
        self._closing = True
        try:
            self.emergency_stop(recording_reason=RECORDING_STOP_APP_CLOSE)
            self._save_notes_if_changed()
            # Input is off; refuse new skills and wait briefly for a running one.
            self.executor.shutdown()
            self.stop_capture(silent=True)
            if getattr(self, "hotkey_listener", None):
                self.hotkey_listener.stop()
            # Bounded: give the writer a moment to finalise session.json. The
            # recording thread only queues status reports and never calls into
            # Tk, so it cannot be stuck waiting on this thread.
            if not self.recorder.wait_stopped(RECORDING_CLOSE_WAIT_SECONDS):
                logging.getLogger(__name__).warning(
                    "Recording was still closing when the app exited."
                )
        finally:
            self._planner_logger.removeHandler(self._planner_log_handler)
            self.root.destroy()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    app = PersonalGameAIApp(root)
    root.mainloop()
