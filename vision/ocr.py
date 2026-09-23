from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

import numpy as np


ROI: TypeAlias = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Text recognized by an OCR backend and its normalized confidence."""

    text: str
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("OCR text must be a string.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR confidence must be between 0.0 and 1.0.")


class OcrEngine(Protocol):
    """Pluggable OCR backend, deliberately independent of capture or UI code."""

    def read_text(
        self,
        image: np.ndarray,
        *,
        whitelist: str | None = None,
    ) -> OcrResult:
        """Recognize text in one already-cropped image."""


class PytesseractEngine:
    """OCR backend using the optional pytesseract package and Tesseract binary."""

    def __init__(self, *, page_segmentation_mode: int = 7) -> None:
        if page_segmentation_mode <= 0:
            raise ValueError("page_segmentation_mode must be positive.")
        self._page_segmentation_mode = page_segmentation_mode

    def read_text(
        self,
        image: np.ndarray,
        *,
        whitelist: str | None = None,
    ) -> OcrResult:
        if image is None or image.size == 0:
            raise ValueError("OCR image is empty.")

        try:
            import pytesseract
        except ImportError as error:
            raise RuntimeError(
                "PytesseractEngine requires the optional 'pytesseract' package and "
                "a Tesseract installation."
            ) from error

        config = f"--psm {self._page_segmentation_mode}"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"

        data = pytesseract.image_to_data(
            image,
            config=config,
            output_type=pytesseract.Output.DICT,
        )
        words: list[str] = []
        confidences: list[float] = []
        for text, confidence in zip(data["text"], data["conf"], strict=True):
            word = str(text).strip()
            if not word:
                continue
            try:
                value = float(confidence)
            except (TypeError, ValueError):
                value = 0.0
            if not np.isfinite(value) or value < 0.0:
                value = 0.0
            words.append(word)
            confidences.append(value / 100.0)

        if not words or not confidences:
            return OcrResult(text="", confidence=0.0)

        confidence = float(np.clip(np.mean(confidences), 0.0, 1.0))
        return OcrResult(text=" ".join(words), confidence=confidence)


@dataclass(frozen=True, slots=True)
class OcrSpec:
    """Configuration for one named screen region containing text."""

    name: str
    roi: ROI
    whitelist: str | None = None
    min_confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("OCR name cannot be empty.")
        _x, _y, width, height = self.roi
        if width <= 0 or height <= 0:
            raise ValueError("OCR ROI width/height must be positive.")
        if self.whitelist is not None and not self.whitelist:
            raise ValueError("OCR whitelist cannot be empty when provided.")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0.")


@dataclass(frozen=True, slots=True)
class OcrMeasurement:
    """One configured OCR result, ready to be written to GameState."""

    name: str
    valid: bool
    text: str
    confidence: float
    bbox: ROI | None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("OCR measurement name cannot be empty.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR measurement confidence must be between 0.0 and 1.0.")
        if self.valid and (not self.text or self.bbox is None):
            raise ValueError("A valid OCR measurement requires text and a bbox.")
        if not self.valid and (self.text or self.bbox is not None):
            raise ValueError("An invalid OCR measurement cannot include text or a bbox.")


def measure_ocr(
    frame: np.ndarray,
    engine: OcrEngine,
    spec: OcrSpec,
) -> OcrMeasurement:
    """Crop a configured ROI, recognize text, and apply local safety filtering."""

    if frame is None or frame.size == 0:
        raise ValueError("Frame image is empty.")

    frame_height, frame_width = frame.shape[:2]
    roi_x, roi_y, roi_width, roi_height = spec.roi
    left = max(0, roi_x)
    top = max(0, roi_y)
    right = min(frame_width, roi_x + roi_width)
    bottom = min(frame_height, roi_y + roi_height)
    if right <= left or bottom <= top:
        return _invalid_measurement(spec.name, confidence=0.0)

    result = engine.read_text(frame[top:bottom, left:right], whitelist=spec.whitelist)
    text = " ".join(result.text.split())
    if spec.whitelist is not None:
        text = "".join(character for character in text if character in spec.whitelist)

    if not text or result.confidence < spec.min_confidence:
        return _invalid_measurement(spec.name, confidence=result.confidence)

    return OcrMeasurement(
        name=spec.name,
        valid=True,
        text=text,
        confidence=result.confidence,
        bbox=(left, top, right - left, bottom - top),
    )


def _invalid_measurement(name: str, *, confidence: float) -> OcrMeasurement:
    return OcrMeasurement(
        name=name,
        valid=False,
        text="",
        confidence=confidence,
        bbox=None,
    )
