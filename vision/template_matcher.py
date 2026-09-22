from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


@dataclass
class MatchResult:
    score: float
    x: int
    y: int
    w: int
    h: int


class TemplateMatcher:
    def __init__(self, threshold: float = 0.82):
        self.threshold = float(threshold)
        self.template_bgr: Optional[np.ndarray] = None
        self.template_gray: Optional[np.ndarray] = None
        self.template_path: Optional[Path] = None

    @property
    def loaded(self) -> bool:
        return self.template_gray is not None

    @property
    def size(self) -> tuple[int, int]:
        if self.template_gray is None:
            return (0, 0)
        h, w = self.template_gray.shape[:2]
        return (w, h)

    def clear(self) -> None:
        self.template_bgr = None
        self.template_gray = None
        self.template_path = None

    def load_array(self, image_bgr: np.ndarray, path: str | Path | None = None) -> None:
        if image_bgr is None or image_bgr.size == 0:
            raise ValueError("Template image is empty.")
        if image_bgr.shape[0] < 8 or image_bgr.shape[1] < 8:
            raise ValueError("Template must be at least 8x8 pixels.")
        self.template_bgr = image_bgr.copy()
        self.template_gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        self.template_path = None if path is None else Path(path)

    def load_file(self, path: str | Path) -> None:
        p = Path(path)
        image = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"Could not read template: {p}")
        self.load_array(image, p)

    def find_best(self, frame_bgr: np.ndarray) -> Optional[MatchResult]:
        if self.template_gray is None:
            return None

        frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        th, tw = self.template_gray.shape[:2]
        fh, fw = frame_gray.shape[:2]

        if th > fh or tw > fw:
            return None

        result = cv2.matchTemplate(
            frame_gray,
            self.template_gray,
            cv2.TM_CCOEFF_NORMED
        )
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)

        if float(max_val) < self.threshold:
            return None

        return MatchResult(
            score=float(max_val),
            x=int(max_loc[0]),
            y=int(max_loc[1]),
            w=int(tw),
            h=int(th),
        )
