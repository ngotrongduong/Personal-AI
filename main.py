from __future__ import annotations

from dataclasses import dataclass
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
from agent.game_state import GameState
from agent.ollama_client import OllamaClientConfig
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.planner_scheduler import PlannerCycleReport
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
from agent.skill_executor import SkillExecutor
from agent.skills import ClickSkill, HoldSkill, PressSkill, Skill, SkillBook, SkillPermissions
from agent.vision_state_bridge import apply_detections
from core.capture import WindowCapture
from core.input_controller import InputController
from core.window_utils import list_visible_windows, client_region, focus_window
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


APP_VERSION = "0.5.0"
PLANNER_DEFAULT_MODEL = "qwen3.5:9b"
PLANNER_NO_CYCLE_TEXT = "Last cycle: —"
PLANNER_MESSAGE_MAX_CHARS = 100

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
    """Forward scheduler-thread planner logs onto Tk's event loop."""

    def __init__(self, app: PersonalGameAIApp) -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        if self._app._closing:
            return
        try:
            self._app.root.after(0, self._log_if_open, message)
        except (tk.TclError, RuntimeError):
            pass

    def _log_if_open(self, message: str) -> None:
        if not self._app._closing:
            self._app.log(f"Planner: {message}")


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
        ttk.Label(
            planner_box, textvariable=self.planner_status_var
        ).pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Label(
            planner_box, textvariable=self.planner_last_cycle_var
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
            self.status_var.set("CAPTURING")
            self.log(f"Capture started: {win32gui.GetWindowText(hwnd)}")
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
        self.planner.stop()
        self._reset_planner_last_cycle()
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

    def _stop_planner_for(self, why: str):
        planner_was_running = self.planner.is_running
        self.planner.stop()
        self._reset_planner_last_cycle()
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
                planner=self.profile.planner if self.profile is not None else None,
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

    def _toggle_planner(self):
        if not self.planner_enabled_var.get():
            self.planner.stop()
            self._reset_planner_last_cycle()
            self.planner_status_var.set("Planner: disabled.")
            self.log("LLM planner DISABLED.")
            return

        try:
            interval_seconds = float(self.planner_interval_var.get())
            if not math.isfinite(interval_seconds) or interval_seconds < 1.0:
                raise ValueError("Planner interval must be a finite value of at least 1.0 seconds.")
            config = PlannerConfig(
                enabled=True,
                ollama=OllamaClientConfig(model=self.planner_model_var.get().strip()),
                interval_seconds=interval_seconds,
            )
            self._reset_planner_last_cycle()
            generation = self._planner_generation
            self.planner.start(
                self.rule_engine,
                config,
                on_cycle=lambda report: self._schedule_planner_cycle_report(generation, report),
            )
        except ValueError as exc:
            self.planner_enabled_var.set(False)
            self.planner_status_var.set("Planner: disabled.")
            messagebox.showerror("Enable LLM planner", str(exc))
            return

        self.planner_status_var.set(
            f"Planner: enabled ({config.ollama.model}, every {config.interval_seconds:g}s)."
        )
        self.log(
            "LLM planner ENABLED. It can only enable or disable existing rules; "
            "input control still gates every dispatch."
        )

    def _reset_planner_last_cycle(self):
        """Invalidate pending cycle reports and clear the last-cycle label (Tk thread)."""
        self._planner_generation += 1
        self.planner_last_cycle_var.set(PLANNER_NO_CYCLE_TEXT)

    def _schedule_planner_cycle_report(self, generation: int, report: PlannerCycleReport):
        """Called on the planner thread; marshal the report onto Tk's event loop."""
        if self._closing:
            return
        try:
            self.root.after(0, self._show_planner_cycle_report, generation, report)
        except (tk.TclError, RuntimeError):
            pass

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

        if self.capture:
            frame = self.capture.latest_frame()
            self.fps_var.set(f"Capture: {self.capture.actual_fps:.1f} FPS")

            if self.capture.last_error:
                self.status_var.set(f"Capture error: {self.capture.last_error}")

            if frame is not None:
                self.latest_raw_frame = frame
                self._run_vision_if_due(frame)
                self._draw_preview(frame)

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
