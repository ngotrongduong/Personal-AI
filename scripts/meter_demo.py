"""Harmless deterministic window for the v1.1 meter smoke test.

The demo never sends input. It only displays one color-coded HP bar and reacts
to ordinary keyboard/button input, making it a safe target for live capture.

Known client-frame meter:
    name: hp
    roi: [60, 80, 400, 32]
    HSV fill: H 50-70, S 180-255, V 180-255 (OpenCV)
    direction: left_to_right

Keys:
    H  heal +20%
    D  damage -20%
    R  reset to 50%
    0  set 0%
    1  set 20%
    2  set 50%
    3  set 80%
    4  set 100%
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import tkinter as tk
from tkinter import ttk


WINDOW_TITLE = "Personal Game AI - Meter Demo"
CLIENT_WIDTH = 560
CLIENT_HEIGHT = 280

BAR_X = 60
BAR_Y = 80
BAR_WIDTH = 400
BAR_HEIGHT = 32
METER_ROI = (BAR_X, BAR_Y, BAR_WIDTH, BAR_HEIGHT)

FILL_COLOR = "#00ff00"
EMPTY_COLOR = "#161616"
TEXT_COLOR = "#f0f0f0"
WINDOW_BG = "#242424"

HSV_LOWER = (50, 180, 180)
HSV_UPPER = (70, 255, 255)

START_FRACTION = 0.50
STEP_FRACTION = 0.20


@dataclass(slots=True)
class MeterDemoState:
    """Small pure state model kept separate from Tk for deterministic tests."""

    fraction: float = START_FRACTION

    def __post_init__(self) -> None:
        self.fraction = clamp_fraction(self.fraction)

    def set_fraction(self, value: float) -> float:
        self.fraction = clamp_fraction(value)
        return self.fraction

    def adjust(self, delta: float) -> float:
        return self.set_fraction(self.fraction + delta)

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100.0))


def clamp_fraction(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("meter fraction must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("meter fraction must be a finite number.")
    return min(1.0, max(0.0, number))


class MeterDemoApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = MeterDemoState()

        root.title(WINDOW_TITLE)
        root.geometry(f"{CLIENT_WIDTH}x{CLIENT_HEIGHT}")
        root.resizable(False, False)
        root.configure(bg=WINDOW_BG)

        tk.Label(
            root,
            text="v1.1 Meter Smoke-Test Target",
            bg=WINDOW_BG,
            fg=TEXT_COLOR,
            font=("Segoe UI", 14, "bold"),
        ).place(x=60, y=24)

        self.value_var = tk.StringVar()
        tk.Label(
            root,
            textvariable=self.value_var,
            bg=WINDOW_BG,
            fg=TEXT_COLOR,
            font=("Consolas", 11),
        ).place(x=400, y=30)

        self.bar = tk.Canvas(
            root,
            width=BAR_WIDTH,
            height=BAR_HEIGHT,
            bg=EMPTY_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self.bar.place(x=BAR_X, y=BAR_Y)
        self.fill_id = self.bar.create_rectangle(
            0,
            0,
            int(BAR_WIDTH * self.state.fraction),
            BAR_HEIGHT,
            fill=FILL_COLOR,
            outline="",
        )

        self.status_var = tk.StringVar(
            value="H: +20%   D: -20%   R: reset   0/1/2/3/4: fixed values"
        )
        tk.Label(
            root,
            textvariable=self.status_var,
            bg=WINDOW_BG,
            fg=TEXT_COLOR,
            font=("Segoe UI", 10),
        ).place(x=60, y=125)

        controls = ttk.Frame(root)
        controls.place(x=60, y=160)
        for text, value in (
            ("0%", 0.0),
            ("20%", 0.2),
            ("50%", 0.5),
            ("80%", 0.8),
            ("100%", 1.0),
        ):
            ttk.Button(
                controls,
                text=text,
                width=8,
                command=lambda v=value: self.set_fraction(v, f"button {int(v * 100)}%"),
            ).pack(side="left", padx=(0, 6))

        tk.Label(
            root,
            text=f"Client ROI: {list(METER_ROI)}   Fill: {FILL_COLOR}",
            bg=WINDOW_BG,
            fg="#bdbdbd",
            font=("Consolas", 9),
        ).place(x=60, y=215)

        root.bind("<KeyPress-h>", lambda _event: self.adjust(+STEP_FRACTION, "key H (heal)"))
        root.bind("<KeyPress-H>", lambda _event: self.adjust(+STEP_FRACTION, "key H (heal)"))
        root.bind("<KeyPress-d>", lambda _event: self.adjust(-STEP_FRACTION, "key D (damage)"))
        root.bind("<KeyPress-D>", lambda _event: self.adjust(-STEP_FRACTION, "key D (damage)"))
        root.bind("<KeyPress-r>", lambda _event: self.set_fraction(START_FRACTION, "key R"))
        root.bind("<KeyPress-R>", lambda _event: self.set_fraction(START_FRACTION, "key R"))
        for key, value in (("0", 0.0), ("1", 0.2), ("2", 0.5), ("3", 0.8), ("4", 1.0)):
            root.bind(
                f"<KeyPress-{key}>",
                lambda _event, v=value, k=key: self.set_fraction(v, f"key {k}"),
            )

        self._redraw("start")

    def set_fraction(self, value: float, reason: str) -> None:
        self.state.set_fraction(value)
        self._redraw(reason)

    def adjust(self, delta: float, reason: str) -> None:
        self.state.adjust(delta)
        self._redraw(reason)

    def _redraw(self, reason: str) -> None:
        width = int(round(BAR_WIDTH * self.state.fraction))
        self.bar.coords(self.fill_id, 0, 0, width, BAR_HEIGHT)
        self.value_var.set(f"HP {self.state.percent:3d}%")
        self.status_var.set(
            f"Last: {reason}   |   H: +20%   D: -20%   R: reset"
        )


def main() -> None:
    print("Personal Game AI meter demo")
    print(f"window title: {WINDOW_TITLE}")
    print(f"client ROI: {list(METER_ROI)}")
    print(f"HSV lower/upper: {list(HSV_LOWER)} / {list(HSV_UPPER)}")
    print("keys: H heal +20%, D damage -20%, R reset, 0/1/2/3/4 fixed values")

    root = tk.Tk()
    MeterDemoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
