from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vision.ocr import OcrEngine, OcrResult, OcrSpec, PytesseractEngine, measure_ocr


class FakeOcrEngine(OcrEngine):
    def __init__(self, result: OcrResult) -> None:
        self.result = result
        self.image: np.ndarray | None = None
        self.whitelist: str | None = None

    def read_text(self, image: np.ndarray, *, whitelist: str | None = None) -> OcrResult:
        self.image = image.copy()
        self.whitelist = whitelist
        return self.result


class OcrTests(unittest.TestCase):
    def test_pytesseract_keeps_text_with_negative_confidence(self) -> None:
        calls: list[dict[str, object]] = []

        def image_to_data(_image: np.ndarray, **kwargs: object) -> dict[str, list[object]]:
            calls.append(kwargs)
            return {
                "text": ["", "742", "99"],
                "conf": ["-1", "-1", "80"],
            }

        pytesseract = SimpleNamespace(
            Output=SimpleNamespace(DICT=object()),
            image_to_data=image_to_data,
        )
        with patch.dict(sys.modules, {"pytesseract": pytesseract}):
            result = PytesseractEngine().read_text(
                np.zeros((10, 10, 3), dtype=np.uint8),
                whitelist="0123456789",
            )

        self.assertEqual(result.text, "742 99")
        self.assertEqual(result.confidence, 0.4)
        self.assertEqual(calls[0]["config"], "--psm 7 -c tessedit_char_whitelist=0123456789")

    def test_pytesseract_returns_empty_result_for_empty_entries(self) -> None:
        pytesseract = SimpleNamespace(
            Output=SimpleNamespace(DICT=object()),
            image_to_data=lambda _image, **_kwargs: {"text": ["", "  "], "conf": ["-1", "-1"]},
        )
        with patch.dict(sys.modules, {"pytesseract": pytesseract}):
            result = PytesseractEngine().read_text(np.zeros((10, 10, 3), dtype=np.uint8))

        self.assertEqual(result, OcrResult(text="", confidence=0.0))

    def test_crops_roi_before_calling_engine(self) -> None:
        frame = np.arange(10 * 12 * 3, dtype=np.uint8).reshape(10, 12, 3)
        engine = FakeOcrEngine(OcrResult("Ready", 0.95))

        measurement = measure_ocr(frame, engine, OcrSpec("status", (2, 3, 5, 4)))

        self.assertTrue(measurement.valid)
        self.assertEqual(measurement.text, "Ready")
        self.assertEqual(measurement.bbox, (2, 3, 5, 4))
        self.assertIsNotNone(engine.image)
        assert engine.image is not None
        np.testing.assert_array_equal(engine.image, frame[3:7, 2:7])

    def test_digit_whitelist_is_forwarded_and_applied_after_recognition(self) -> None:
        engine = FakeOcrEngine(OcrResult("HP: 12/34", 0.95))

        measurement = measure_ocr(
            np.zeros((10, 10, 3), dtype=np.uint8),
            engine,
            OcrSpec("hp", (0, 0, 10, 10), whitelist="0123456789"),
        )

        self.assertEqual(engine.whitelist, "0123456789")
        self.assertTrue(measurement.valid)
        self.assertEqual(measurement.text, "1234")

    def test_low_confidence_result_is_invalid(self) -> None:
        engine = FakeOcrEngine(OcrResult("42", 0.49))

        measurement = measure_ocr(
            np.zeros((10, 10, 3), dtype=np.uint8),
            engine,
            OcrSpec("gold", (0, 0, 10, 10), min_confidence=0.50),
        )

        self.assertFalse(measurement.valid)
        self.assertEqual(measurement.text, "")
        self.assertIsNone(measurement.bbox)
        self.assertEqual(measurement.confidence, 0.49)

    def test_outside_roi_does_not_call_engine(self) -> None:
        engine = FakeOcrEngine(OcrResult("unreachable", 1.0))

        measurement = measure_ocr(
            np.zeros((10, 10, 3), dtype=np.uint8),
            engine,
            OcrSpec("offscreen", (20, 20, 5, 5)),
        )

        self.assertFalse(measurement.valid)
        self.assertIsNone(engine.image)


if __name__ == "__main__":
    unittest.main()
