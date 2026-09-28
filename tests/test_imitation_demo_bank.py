from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import cv2
import numpy as np

from imitation.demo_bank import DemoBank, extract_demo_clicks, select_sessions
from recording.schema import (
    FORMAT_VERSION,
    FrameEvent,
    MarkerEvent,
    MouseButtonEvent,
    SessionInfo,
    event_to_dict,
    session_to_dict,
)


def make_frame(button_x: int = 30, color: tuple[int, int, int] = (20, 180, 240)) -> np.ndarray:
    frame = np.zeros((80, 120, 3), dtype=np.uint8)
    frame[:, :] = (18, 24, 30)
    frame[10:25, :] = (80, 30, 20)
    frame[40:65, button_x : button_x + 28] = color
    return frame


def write_session(
    root: Path,
    name: str,
    frame: np.ndarray,
    *,
    point: tuple[int, int] = (44, 52),
    window_title: str = "Merchant Guilds - Test",
    click_t: float = 0.20,
) -> Path:
    folder = root / name
    frames = folder / "frames"
    frames.mkdir(parents=True)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    encoded.tofile(frames / "000001.jpg")
    x, y = point
    events = [
        MarkerEvent(0.0, "start"),
        FrameEvent(0.10, 1, "frames/000001.jpg"),
        MouseButtonEvent(click_t, "left", "down", x, y),
        MouseButtonEvent(click_t + 0.01, "left", "up", x, y),
        MarkerEvent(click_t + 0.10, "stop", "user"),
    ]
    timestamp = datetime(2026, 9, 28, tzinfo=timezone.utc).isoformat()
    info = SessionInfo(
        format_version=FORMAT_VERSION,
        app_version="1.3.0",
        window_title=window_title,
        client_width=120,
        client_height=80,
        record_fps=10.0,
        started_at=timestamp,
        ended_at=timestamp,
        frames=1,
        dropped_frames=0,
        events=len(events),
        stop_reason="user",
    )
    (folder / "session.json").write_text(
        json.dumps(session_to_dict(info)), encoding="utf-8"
    )
    (folder / "events.jsonl").write_text(
        "".join(json.dumps(event_to_dict(event)) + "\n" for event in events),
        encoding="utf-8",
    )
    return folder


def test_extracts_pre_click_frame_and_normalised_point(tmp_path) -> None:
    folder = write_session(tmp_path, "session_a", make_frame())
    result = extract_demo_clicks(folder)

    assert result.skipped == {}
    assert len(result.clicks) == 1
    click = result.clicks[0]
    assert click.session == "session_a"
    assert click.frame_index == 1
    assert click.frame_path == folder / "frames/000001.jpg"
    assert np.isclose(click.fx, 44 / 120)
    assert np.isclose(click.fy, 52 / 80)
    assert click.screen.shape == (48 * 48,)
    assert click.patch.shape == (24, 24)


def test_select_sessions_and_bank_counts_are_sorted(tmp_path) -> None:
    second = write_session(tmp_path, "b", make_frame(button_x=60))
    first = write_session(tmp_path, "a", make_frame())
    write_session(tmp_path, "other", make_frame(), window_title="Notepad")

    assert select_sessions(tmp_path, "merchant") == [first, second]
    assert select_sessions(tmp_path, "Guilds", ("b",)) == [second]
    bank = DemoBank.build([first, second])
    assert len(bank.clicks) == 2
    assert bank.session_counts == {"a": 1, "b": 1}
    assert bank.screens.shape == (2, 48 * 48)


def test_invalid_session_is_skipped_whole(tmp_path) -> None:
    folder = write_session(tmp_path, "broken", make_frame())
    (folder / "frames" / "000001.jpg").unlink()
    result = extract_demo_clicks(folder)
    assert result.clicks == ()
    assert result.skipped == {"invalid_session": 1}
