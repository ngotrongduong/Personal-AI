from __future__ import annotations

from dataclasses import dataclass
import time
import win32con
import win32gui


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str

    def label(self) -> str:
        return f"{self.title}  [HWND {self.hwnd}]"


def list_visible_windows() -> list[WindowInfo]:
    items: list[WindowInfo] = []

    def callback(hwnd: int, _extra) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            if right - left < 64 or bottom - top < 64:
                return
        except Exception:
            return
        items.append(WindowInfo(hwnd=hwnd, title=title))

    win32gui.EnumWindows(callback, None)
    items.sort(key=lambda w: w.title.lower())
    return items


def client_region(hwnd: int) -> tuple[int, int, int, int]:
    """Return client-area region in screen coordinates: (left, top, right, bottom)."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))

    if screen_right <= screen_left or screen_bottom <= screen_top:
        raise RuntimeError("Selected window has no visible client area.")
    return screen_left, screen_top, screen_right, screen_bottom


def window_exists(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))


def is_foreground(hwnd: int) -> bool:
    """True only if `hwnd` is the current foreground window; False on any error.

    A null handle is never foreground: GetForegroundWindow() returns 0 while
    focus is changing or on the secure desktop.
    """
    if not hwnd:
        return False
    try:
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def focus_window(hwnd: int, settle_seconds: float = 0.12) -> bool:
    """Bring a normal desktop window to the foreground for user-initiated input testing."""
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("Selected window no longer exists.")

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(max(0.03, min(float(settle_seconds), 0.5)))
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as exc:
        raise RuntimeError(f"Could not focus selected window: {exc}") from exc
