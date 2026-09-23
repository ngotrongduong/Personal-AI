from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
from PIL import Image, ImageTk
from pynput import keyboard
import win32gui

from agent.action_dispatcher import ActionDispatcher
from agent.game_state import GameState
from agent.ollama_client import OllamaClientConfig
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.rule_engine import RuleEngine, VisibilityRule
from agent.vision_state_bridge import apply_detections
from core.capture import WindowCapture
from core.input_controller import InputController
from core.window_utils import list_visible_windows, client_region, focus_window
from vision.detector_registry import DetectorRegistry, DetectorSpec
from vision.template_matcher import TemplateMatcher, MatchResult


APP_VERSION = "0.2.0"
PLANNER_DEFAULT_MODEL = "qwen3.5:9b"


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
        self.dispatcher = ActionDispatcher(self.input)
        self.planner = PlannerController(self.game_state)

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

        self.rule_name_var = tk.StringVar(value="rule_1")
        self.rule_detector_var = tk.StringVar(value="")
        self.rule_min_confidence_var = tk.DoubleVar(value=0.82)
        self.rules_var = tk.StringVar(value="Rules: 0 active. Input control still gates every dispatch.")

        self.planner_enabled_var = tk.BooleanVar(value=False)
        self.planner_model_var = tk.StringVar(value=PLANNER_DEFAULT_MODEL)
        self.planner_interval_var = tk.StringVar(value="5.0")
        self.planner_status_var = tk.StringVar(value="Planner: disabled.")

        self._build_ui()
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

        vision_box = ttk.LabelFrame(outer, text="Vision v0.2 — Template detector")
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
        ).pack(anchor="w", padx=8, pady=(0, 7))

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
        self.log("Vision v0.2 ready. Input control starts DISABLED.")

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

    def stop_capture(self, silent: bool = False):
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
        self.input.set_enabled(self.control_var.get())
        if self.control_var.get():
            self.status_var.set("INPUT ENABLED")
            self.log("Keyboard/mouse control ENABLED.")
        else:
            if self.status_var.get() != "EMERGENCY STOP (F8)":
                self.status_var.set("CAPTURING" if self.capture else "READY")
            self.log("Keyboard/mouse control DISABLED; generated inputs released.")

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

    def emergency_stop(self):
        # Release input first; nothing should delay disabling input.
        self.input.set_enabled(False)
        self.control_var.set(False)
        self.status_var.set("EMERGENCY STOP (F8)")
        planner_was_running = self.planner.is_running
        self.planner.stop()
        self.planner_enabled_var.set(False)
        self.planner_status_var.set("Planner: disabled by emergency stop.")
        if planner_was_running:
            self.log("LLM planner stopped by emergency stop.")
        self.log("EMERGENCY STOP: generated inputs released; input control disabled.")

    def _start_hotkey_listener(self):
        def on_press(key):
            if key == keyboard.Key.f8:
                self.root.after(0, self.emergency_stop)

        self.hotkey_listener = keyboard.Listener(on_press=on_press)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

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
        if self.vision_enabled_var.get() and not self.matcher.loaded:
            self.vision_enabled_var.set(False)
            messagebox.showwarning(
                "No template",
                "Select a template on the preview first."
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
        planner_was_running = self.planner.is_running
        self.planner.stop()
        self.planner_enabled_var.set(False)
        self.planner_status_var.set("Planner: disabled because rules were cleared.")
        if planner_was_running:
            self.log("LLM planner stopped because rules were cleared.")
        self.rule_engine = RuleEngine()
        self.rules_var.set("Rules: 0 active.")
        self.log("All rules cleared.")

    def _toggle_planner(self):
        if not self.planner_enabled_var.get():
            self.planner.stop()
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
            self.planner.start(self.rule_engine, config)
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
                self._registry_visibility.pop(detector_name, None)
                spec = DetectorSpec(name=detector_name, threshold=self.threshold_var.get())
                self.registry.register_array(spec, roi)
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
                    result = self.dispatcher.dispatch(intent, hwnd=hwnd)
                    last_outcome = "dispatched" if result.dispatched else "blocked"
                    self.log(
                        f"Rule '{intent.rule_name}' target={intent.detector_name} "
                        f"confidence={intent.confidence:.2f} -> "
                        f"{last_outcome.upper()}: {result.reason}"
                    )

                self.rules_var.set(
                    f"Rules: {len(self.rule_engine.rules)} active. "
                    f"Last: '{intents[-1].rule_name}' {last_outcome}."
                )

    # ---------------- Preview ----------------

    def _poll_preview(self):
        if self._closing:
            return

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
            self.emergency_stop()
            self.stop_capture(silent=True)
            if getattr(self, "hotkey_listener", None):
                self.hotkey_listener.stop()
        finally:
            self._planner_logger.removeHandler(self._planner_log_handler)
            self.root.destroy()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    app = PersonalGameAIApp(root)
    root.mainloop()
