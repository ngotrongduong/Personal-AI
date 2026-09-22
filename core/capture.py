from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import dxcam
import numpy as np

from .window_utils import client_region, window_exists


class WindowCapture:
    def __init__(self, hwnd: int, target_fps: int = 30):
        self.hwnd = hwnd
        self.target_fps = max(1, min(int(target_fps), 120))
        self._camera = dxcam.create(output_color="BGR")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._latest: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self.actual_fps = 0.0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        try:
            self._camera.release()
        except Exception:
            pass

    def latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def _loop(self) -> None:
        interval = 1.0 / self.target_fps
        count = 0
        stamp = time.perf_counter()

        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                if not window_exists(self.hwnd):
                    self.last_error = "Selected window no longer exists."
                    break

                region = client_region(self.hwnd)
                frame = self._camera.grab(region=region)
                if frame is not None:
                    with self._lock:
                        self._latest = frame
                    count += 1

                now = time.perf_counter()
                if now - stamp >= 1.0:
                    self.actual_fps = count / (now - stamp)
                    count = 0
                    stamp = now
                    self.last_error = None

            except Exception as exc:
                self.last_error = str(exc)
                time.sleep(0.25)

            elapsed = time.perf_counter() - started
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
