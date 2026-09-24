"""Lifecycle owner for one demonstration recording at a time.

``RecordingController.start`` returns immediately: creating the session folder,
starting the input listeners and sampling frames all happen on a daemon
"recording-sampler" thread, so the Tk thread never touches the disk.
``stop`` is non-blocking too; the sampler thread stops the listeners, writes a
stop marker and lets ``SessionWriter`` flush on its own thread.

The controller only observes: it reads frames from the capture, snapshots
``GameState`` and records the player's own input through ``InputRecorder``.
It never sends input and has no handle to the rule engine or dispatcher.
Recording and autonomous input control are mutually exclusive: ``start`` is
refused while ``input_control_enabled()`` is true, and a running session
stops itself as soon as it becomes true.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Protocol

from recording.input_recorder import InputRecorder, Region
from recording.schema import MarkerEvent, ObservationRecord, StateEvent, observation_to_record
from recording.session_writer import SessionWriter


logger = logging.getLogger(__name__)

MIN_FPS = 1.0
MAX_FPS = 30.0
DEFAULT_FPS = 10.0
DEFAULT_MAX_DURATION_SECONDS = 30 * 60.0
DEFAULT_MIN_FREE_BYTES = 1024**3

STOP_USER = "user"
STOP_MAX_DURATION = "max_duration"
STOP_LOW_DISK = "low_disk"
STOP_WINDOW_CLOSED = "window_closed"
STOP_INPUT_CONTROL = "input_control_enabled"
STOP_ERROR = "error"


class RecordingRefusedError(RuntimeError):
    """Raised by ``start`` when recording is not allowed right now."""


class FrameSource(Protocol):
    def latest_frame(self) -> Any: ...


class StateSource(Protocol):
    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RecordingStatus:
    """Observation-only progress report delivered to ``on_status``.

    ``state`` is ``"recording"`` while running, ``"stopping"`` once the input
    listeners are removed and the writer is flushing, ``"stopped"`` once the
    session folder is finalised, or ``"failed"`` if the session could not
    start (``session_dir`` is set if a folder was already created and closed).

    Delivered on the "recording-sampler" thread: a UI must marshal it to its
    own thread (e.g. Tk ``root.after``) and never touch widgets directly.
    """

    state: str
    session_dir: Path | None
    elapsed: float
    frames: int
    dropped: int
    events: int
    stop_reason: str | None = None
    error: str | None = None


StatusCallback = Callable[[RecordingStatus], object]


def _default_foreground_window() -> int:
    import win32gui

    return int(win32gui.GetForegroundWindow())


def _default_client_region(hwnd: int) -> Region:
    from core.window_utils import client_region

    return client_region(hwnd)


def _default_window_exists(hwnd: int) -> bool:
    from core.window_utils import window_exists

    return window_exists(hwnd)


def _default_window_title(hwnd: int) -> str:
    import win32gui

    return str(win32gui.GetWindowText(hwnd))


def _default_disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def validate_fps(fps: float) -> float:
    value = float(fps)
    if not MIN_FPS <= value <= MAX_FPS:
        raise ValueError(f"Recording fps must be between {MIN_FPS:g} and {MAX_FPS:g}, got {fps}.")
    return value


def _require_positive_finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value}.")
    return number


class _Session:
    """One recording run; everything except request_stop runs on its thread."""

    def __init__(
        self,
        controller: RecordingController,
        *,
        hwnd: int,
        capture: FrameSource,
        game_state: StateSource,
        root_dir: Path,
        fps: float,
        on_status: StatusCallback | None,
    ) -> None:
        self._c = controller
        self._hwnd = hwnd
        self._capture = capture
        self._game_state = game_state
        self._root_dir = root_dir
        self._fps = fps
        self._on_status = on_status
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stop_reason: str | None = None
        self._session_dir: Path | None = None
        self._status_errors = 0
        self._state_errors = 0
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, name="recording-sampler", daemon=True)

    def request_stop(self, reason: str) -> None:
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
        self._stop.set()

    def _final_reason(self) -> str:
        with self._lock:
            return self._stop_reason or STOP_USER

    def _run(self) -> None:
        try:
            try:
                opened = self._open()
            except Exception as exc:
                logger.exception("Could not start recording.")
                self._report(
                    RecordingStatus(
                        state="failed",
                        session_dir=self._session_dir,
                        elapsed=0.0,
                        frames=0,
                        dropped=0,
                        events=0,
                        stop_reason=STOP_ERROR,
                        error=str(exc) or type(exc).__name__,
                    )
                )
                return
            writer, recorder = opened
            try:
                self._sample_loop(writer, recorder)
            except Exception:
                logger.exception("Recording sampler failed; stopping the session.")
                self.request_stop(STOP_ERROR)
            self._close(writer, recorder)
        finally:
            self.finished.set()

    def _open(self) -> tuple[SessionWriter, InputRecorder]:
        c = self._c
        hwnd = self._hwnd
        left, top, right, bottom = c._client_region(hwnd)
        writer = c._writer_factory(
            self._root_dir,
            app_version=c._app_version,
            window_title=c._window_title(hwnd),
            client_size=(right - left, bottom - top),
            record_fps=self._fps,
            clock=c._clock,
        )
        self._session_dir = writer.session_dir
        try:
            writer.write_event(MarkerEvent(t=self._elapsed(writer), action="start"))
            recorder = c._input_recorder_factory(
                writer.write_event,
                is_target_foreground=lambda: c._foreground_window() == hwnd,
                client_region=lambda: c._client_region(hwnd),
                t0=writer.t0,
                clock=c._clock,
            )
            # A stop requested while the folder was being created must not
            # install the input hooks at all.
            if not self._stop.is_set():
                recorder.start()
        except Exception:
            writer.close(STOP_ERROR)
            writer.wait_finished(c._finalise_timeout)
            raise
        return writer, recorder

    def _elapsed(self, writer: SessionWriter, now: float | None = None) -> float:
        current = self._c._clock() if now is None else now
        return max(0.0, current - writer.t0)

    def _input_control_enabled(self) -> bool:
        try:
            return bool(self._c._input_control_enabled())
        except Exception:
            logger.exception("input_control_enabled() failed; stopping the recording.")
            return True

    def _sample_loop(self, writer: SessionWriter, recorder: InputRecorder) -> None:
        c = self._c
        interval = 1.0 / self._fps
        next_disk_check = 0.0
        next_status = 0.0
        next_sample = c._clock()
        while not self._stop.is_set():
            now = c._clock()
            elapsed = self._elapsed(writer, now)
            if self._input_control_enabled():
                self.request_stop(STOP_INPUT_CONTROL)
                break
            if elapsed >= c._max_duration:
                self.request_stop(STOP_MAX_DURATION)
                break
            if not c._window_exists(self._hwnd):
                self.request_stop(STOP_WINDOW_CLOSED)
                break
            if elapsed >= next_disk_check:
                next_disk_check = elapsed + c._disk_check_interval
                if c._disk_free(writer.session_dir) < c._min_free_bytes:
                    logger.warning("Free disk space is low; stopping the recording.")
                    self.request_stop(STOP_LOW_DISK)
                    break

            recorder.poll_focus()
            # WindowCapture.latest_frame() returns a fresh copy made under its
            # lock, so the writer thread owns this array.
            frame = self._capture.latest_frame()
            if frame is not None:
                writer.write_frame(frame, elapsed, self._state_event(elapsed, writer.t0))

            if elapsed >= next_status:
                next_status = elapsed + c._status_interval
                self._report(self._status("recording", writer, elapsed))

            next_sample += interval
            current = c._clock()
            if next_sample < current:
                # Fell behind (slow snapshot or a paused clock): skip ahead
                # instead of bursting to catch up.
                next_sample = current + interval
            self._stop.wait(max(0.0, next_sample - current))

    def _state_event(self, t: float, t0: float) -> StateEvent:
        records: list[ObservationRecord] = []
        for name, observation in sorted(self._game_state.snapshot().items()):
            try:
                records.append(observation_to_record(observation, t0))
            except Exception:
                self._state_errors += 1
                if self._state_errors == 1:
                    logger.exception("Skipping observation %r that cannot be recorded.", name)
        return StateEvent(t=t, observations=tuple(records))

    def _close(self, writer: SessionWriter, recorder: InputRecorder) -> None:
        reason = self._final_reason()
        recorder.stop()
        try:
            writer.write_event(MarkerEvent(t=self._elapsed(writer), action="stop", reason=reason))
        except Exception:
            logger.exception("Could not write the recording stop marker.")
        elapsed = self._elapsed(writer)
        writer.close(reason)
        self._report(self._status("stopping", writer, elapsed, stop_reason=reason))
        if not writer.wait_finished(self._c._finalise_timeout):
            logger.warning("Recording writer is still flushing %s.", writer.session_dir)
        self._report(self._status("stopped", writer, elapsed, stop_reason=reason))

    def _status(
        self, state: str, writer: SessionWriter, elapsed: float, *, stop_reason: str | None = None
    ) -> RecordingStatus:
        stats = writer.stats()
        return RecordingStatus(
            state=state,
            session_dir=writer.session_dir,
            elapsed=elapsed,
            frames=stats.frames_written,
            dropped=stats.dropped_frames,
            events=stats.events_written,
            stop_reason=stop_reason,
        )

    def _report(self, status: RecordingStatus) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(status)
        except Exception:
            self._status_errors += 1
            if self._status_errors == 1:
                logger.exception("Recording status callback failed.")


class RecordingController:
    """Start and stop demonstration recordings without blocking the caller.

    ``input_control_enabled`` must be thread-safe (e.g. reading
    ``InputController.enabled``); it is polled on the sampler thread.
    """

    def __init__(
        self,
        *,
        app_version: str,
        input_control_enabled: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        writer_factory: Callable[..., SessionWriter] = SessionWriter,
        input_recorder_factory: Callable[..., InputRecorder] = InputRecorder,
        foreground_window: Callable[[], int] = _default_foreground_window,
        client_region: Callable[[int], Region] = _default_client_region,
        window_exists: Callable[[int], bool] = _default_window_exists,
        window_title: Callable[[int], str] = _default_window_title,
        disk_free: Callable[[Path], int] = _default_disk_free,
        max_duration: float = DEFAULT_MAX_DURATION_SECONDS,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        disk_check_interval: float = 5.0,
        status_interval: float = 0.5,
        finalise_timeout: float = 30.0,
    ) -> None:
        self._max_duration = _require_positive_finite(max_duration, "max_duration")
        self._disk_check_interval = _require_positive_finite(
            disk_check_interval, "disk_check_interval"
        )
        self._status_interval = _require_positive_finite(status_interval, "status_interval")
        self._finalise_timeout = _require_positive_finite(finalise_timeout, "finalise_timeout")
        if isinstance(min_free_bytes, bool) or int(min_free_bytes) < 0:
            raise ValueError("min_free_bytes cannot be negative.")
        self._min_free_bytes = int(min_free_bytes)
        self._app_version = app_version
        self._input_control_enabled = input_control_enabled
        self._clock = clock
        self._writer_factory = writer_factory
        self._input_recorder_factory = input_recorder_factory
        self._foreground_window = foreground_window
        self._client_region = client_region
        self._window_exists = window_exists
        self._window_title = window_title
        self._disk_free = disk_free
        self._lock = threading.Lock()
        self._session: _Session | None = None

    @property
    def is_running(self) -> bool:
        """True from start() until the session folder is closed.

        Stays True briefly after stop() while the listeners are removed and
        the writer flushes (reported as ``"stopping"``), so the UI never shows
        "not recording" while an input hook may still be installed.
        """
        with self._lock:
            return self._session is not None and not self._session.finished.is_set()

    def start(
        self,
        hwnd: int,
        capture: FrameSource,
        game_state: StateSource,
        root_dir: Path,
        fps: float = DEFAULT_FPS,
        *,
        on_status: StatusCallback | None = None,
    ) -> bool:
        """Begin recording on a background thread. False if already recording.

        Raises ``RecordingRefusedError`` while autonomous input control is
        enabled and ``ValueError`` for fps outside 1-30. Failures while
        opening the session (bad window, unwritable folder, listener errors)
        are reported through ``on_status`` as ``"failed"``. ``on_status`` runs
        on the sampler thread.
        """
        fps = validate_fps(fps)
        if self._input_control_enabled():
            raise RecordingRefusedError("Recording is not allowed while input control is enabled.")
        with self._lock:
            if self._session is not None and not self._session.finished.is_set():
                return False
            session = _Session(
                self,
                hwnd=int(hwnd),
                capture=capture,
                game_state=game_state,
                root_dir=Path(root_dir),
                fps=fps,
                on_status=on_status,
            )
            self._session = session
            session.thread.start()
        return True

    def stop(self, reason: str = STOP_USER) -> None:
        """Ask the current session to stop. Non-blocking and idempotent."""
        with self._lock:
            session = self._session
        if session is not None:
            session.request_stop(reason)

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait until the current session has closed its folder.

        Never call this on the Tk thread with a long timeout; it exists for
        tests and for a bounded wait during application shutdown.
        """
        with self._lock:
            session = self._session
        return True if session is None else session.finished.wait(timeout)
