"""Background writer for one recording session folder.

Layout: ``<root>/<session>/session.json``, ``frames/000001.jpg`` and
``events.jsonl``. JPEG encoding and all disk writes happen on a daemon writer
thread so callers (the Tk thread, pynput threads) never block on disk.
Frames may be dropped when the queue is full; other events are never dropped.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

from recording.schema import (
    FORMAT_VERSION,
    Event,
    FrameEvent,
    SessionInfo,
    StateEvent,
    event_to_dict,
    session_to_dict,
)


logger = logging.getLogger(__name__)

FrameEncoder = Callable[[Any, int], bytes]

EVENTS_FILE = "events.jsonl"
SESSION_FILE = "session.json"
FRAMES_DIR = "frames"


def encode_jpeg(frame: Any, quality: int) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode failed to encode frame as JPEG.")
    return buffer.tobytes()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class SessionWriterStats:
    frames_written: int
    dropped_frames: int
    events_written: int
    pending: int
    errors: int
    closed: bool
    finished: bool


@dataclass(frozen=True, slots=True)
class _FrameItem:
    t: float
    frame: Any
    state_event: StateEvent | None


class SessionWriter:
    """Owns one session folder and the thread that writes into it."""

    def __init__(
        self,
        root_dir: Path,
        *,
        app_version: str,
        window_title: str,
        client_size: tuple[int, int],
        record_fps: float,
        session_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_queue_frames: int = 64,
        jpeg_quality: int = 85,
        encoder: FrameEncoder | None = None,
    ) -> None:
        if max_queue_frames < 1:
            raise ValueError("max_queue_frames must be at least 1.")
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100.")
        width, height = client_size
        self._info_base = dict(
            format_version=FORMAT_VERSION,
            app_version=app_version,
            window_title=window_title,
            client_width=int(width),
            client_height=int(height),
            record_fps=float(record_fps),
            started_at=_now_iso(),
        )
        # Validates the metadata before anything touches the disk.
        initial_info = self._session_info(ended_at=None, stop_reason=None, counts=(0, 0, 0))

        self._max_queue_frames = max_queue_frames
        self._jpeg_quality = jpeg_quality
        self._encoder: FrameEncoder = encoder or encode_jpeg

        self.session_dir = self._create_session_dir(Path(root_dir), session_name)
        self._frames_dir = self.session_dir / FRAMES_DIR
        self._frames_dir.mkdir()
        self._write_session_json(initial_info)
        self._events_file = open(
            self.session_dir / EVENTS_FILE, "a", encoding="utf-8", newline="\n"
        )
        self.t0 = clock()

        self._cond = threading.Condition()
        self._items: deque[Event | _FrameItem] = deque()
        self._pending_frames = 0
        self._closed = False
        self._stop_reason: str | None = None
        self._frames_written = 0
        self._dropped_frames = 0
        self._events_written = 0
        self._errors = 0
        self._finished = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="recording-session-writer", daemon=True
        )
        self._thread.start()

    @staticmethod
    def _create_session_dir(root_dir: Path, session_name: str | None) -> Path:
        name = session_name or datetime.now().strftime("%Y%m%d_%H%M%S")
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"session_name must be a plain folder name, got {name!r}.")
        root_dir.mkdir(parents=True, exist_ok=True)
        suffix = 1
        while True:
            candidate = root_dir / (name if suffix == 1 else f"{name}_{suffix}")
            try:
                candidate.mkdir()
            except FileExistsError:
                suffix += 1
                continue
            return candidate

    def write_event(self, event: Event) -> bool:
        """Queue a non-frame event. Never dropped; False only after close()."""
        if isinstance(event, FrameEvent):
            raise ValueError("Frame events are created by write_frame().")
        event_to_dict(event)  # Rejects objects that are not schema events.
        with self._cond:
            if self._closed:
                return False
            self._items.append(event)
            self._cond.notify()
        return True

    def write_frame(self, frame: Any, t: float, state_event: StateEvent | None = None) -> bool:
        """Queue a frame (and its paired state). Returns False if dropped or closed."""
        if state_event is not None and not isinstance(state_event, StateEvent):
            raise TypeError("state_event must be a StateEvent or None.")
        FrameEvent(t=t, index=1, file="validate")  # Validates t before queueing.
        with self._cond:
            if self._closed:
                return False
            if self._pending_frames >= self._max_queue_frames:
                self._dropped_frames += 1
                return False
            self._pending_frames += 1
            self._items.append(_FrameItem(t=t, frame=frame, state_event=state_event))
            self._cond.notify()
        return True

    def close(self, stop_reason: str, *, join_timeout: float | None = 0.0) -> bool:
        """Stop accepting writes and let the thread drain and finalise.

        Idempotent. The default ``join_timeout=0.0`` returns immediately (safe
        on the Tk thread); the writer thread still flushes the queue and writes
        ``session.json`` on its own. Returns True once the session has been
        fully written.
        """
        with self._cond:
            if not self._closed:
                self._closed = True
                self._stop_reason = stop_reason
                self._cond.notify_all()
        if join_timeout is None or join_timeout > 0:
            self._thread.join(join_timeout)
        return self._finished.is_set()

    def wait_finished(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def stats(self) -> SessionWriterStats:
        with self._cond:
            return SessionWriterStats(
                frames_written=self._frames_written,
                dropped_frames=self._dropped_frames,
                events_written=self._events_written,
                pending=len(self._items),
                errors=self._errors,
                closed=self._closed,
                finished=self._finished.is_set(),
            )

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    while not self._items and not self._closed:
                        self._cond.wait()
                    if not self._items:
                        break
                    item = self._items.popleft()
                if isinstance(item, _FrameItem):
                    self._process_frame(item)
                    with self._cond:
                        self._pending_frames -= 1
                else:
                    self._append_events((item,))
                with self._cond:
                    idle = not self._items
                if idle:
                    self._flush_events()
        finally:
            with self._cond:
                # If the loop died unexpectedly, stop accepting writes instead
                # of letting the queue grow forever.
                self._closed = True
            self._finalise()

    def _process_frame(self, item: _FrameItem) -> None:
        # Indices are assigned only on success so frame files stay contiguous.
        index = self._frames_written + 1
        relative = f"{FRAMES_DIR}/{index:06d}.jpg"
        try:
            data = self._encoder(item.frame, self._jpeg_quality)
            (self.session_dir / relative).write_bytes(data)
        except Exception:
            logger.exception("Failed to write recording frame; dropping it.")
            with self._cond:
                self._errors += 1
                self._dropped_frames += 1
            return
        with self._cond:
            self._frames_written = index
        events: list[Event] = [FrameEvent(t=item.t, index=index, file=relative)]
        if item.state_event is not None:
            events.append(item.state_event)
        self._append_events(events)

    def _append_events(self, events: tuple[Event, ...] | list[Event]) -> None:
        for event in events:
            try:
                line = json.dumps(event_to_dict(event), ensure_ascii=False)
                self._events_file.write(line + "\n")
            except Exception:
                logger.exception("Failed to append recording event.")
                with self._cond:
                    self._errors += 1
                continue
            with self._cond:
                self._events_written += 1

    def _flush_events(self) -> None:
        try:
            self._events_file.flush()
        except Exception:
            logger.exception("Failed to flush recording events.")
            with self._cond:
                self._errors += 1

    def _finalise(self) -> None:
        try:
            self._flush_events()
            try:
                self._events_file.close()
            except Exception:
                logger.exception("Failed to close recording events file.")
            with self._cond:
                counts = (self._frames_written, self._dropped_frames, self._events_written)
                stop_reason = self._stop_reason
            info = self._session_info(ended_at=_now_iso(), stop_reason=stop_reason, counts=counts)
            self._write_session_json(info)
        except Exception:
            logger.exception("Failed to finalise recording session %s.", self.session_dir)
            with self._cond:
                self._errors += 1
        finally:
            self._finished.set()

    def _session_info(
        self, *, ended_at: str | None, stop_reason: str | None, counts: tuple[int, int, int]
    ) -> SessionInfo:
        frames, dropped, events = counts
        return SessionInfo(
            **self._info_base,
            ended_at=ended_at,
            frames=frames,
            dropped_frames=dropped,
            events=events,
            stop_reason=stop_reason,
        )

    def _write_session_json(self, info: SessionInfo) -> None:
        target = self.session_dir / SESSION_FILE
        temp = target.with_name(SESSION_FILE + ".tmp")
        temp.write_text(
            json.dumps(session_to_dict(info), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, target)
