from __future__ import annotations

import tkinter as tk
import unittest


class DeferredExceptionCallbackTests(unittest.TestCase):
    """
    Regression test for the bug fixed in main.py's _safe_tap_w:
    `except Exception as exc: root.after(0, lambda: use(exc))` raises NameError
    when the deferred lambda actually runs, because Python unbinds `exc` as soon
    as the except block exits. Capturing the message into a plain local variable
    before scheduling the callback avoids this. This needs a real Tk root (the
    bug is specifically about Tk's deferred-callback timing), but takes no
    Windows-specific dependency.
    """

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors: list[BaseException] = []
        self.root.report_callback_exception = self._capture_callback_exception

    def tearDown(self) -> None:
        self.root.destroy()

    def _capture_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        self.errors.append(exc_value)

    def _pump(self, times: int = 5) -> None:
        for _ in range(times):
            self.root.update()

    def test_buggy_pattern_raises_nameerror_in_deferred_callback(self) -> None:
        results: list[str] = []
        try:
            raise ValueError("boom")
        except ValueError as exc:
            # Intentionally the buggy pattern under test: `exc` is unbound by
            # Python once this except block exits, before the deferred lambda
            # below ever runs.
            self.root.after(0, lambda: results.append(f"Input error: {exc}"))  # noqa: F821

        self._pump()

        self.assertEqual(results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], NameError)

    def test_fixed_pattern_delivers_message_to_deferred_callback(self) -> None:
        results: list[str] = []
        try:
            raise ValueError("boom")
        except ValueError as exc:
            message = str(exc)
            self.root.after(0, lambda: results.append(f"Input error: {message}"))

        self._pump()

        self.assertEqual(self.errors, [])
        self.assertEqual(results, ["Input error: boom"])


if __name__ == "__main__":
    unittest.main()
