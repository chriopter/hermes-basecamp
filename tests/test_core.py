from __future__ import annotations

import json
from pathlib import Path

import pytest

from basecamp_platform.core import (
    EventRef,
    SnapshotState,
    build_context_id,
    parse_context_id,
    recording_id_from_url,
)


def test_recording_id_from_url_extracts_api_and_app_urls() -> None:
    assert recording_id_from_url(
        "https://3.basecampapi.com/1111111/buckets/2222222/comments/3333333.json"
    ) == 3333333
    assert recording_id_from_url(
        "https://app.basecamp.com/1111111/buckets/2222222/todos/4444444"
    ) == 4444444
    assert recording_id_from_url("") is None


def test_comment_and_parent_share_item_context() -> None:
    todo = EventRef(
        source="timeline",
        project_id=2222222,
        recording_id=4444444,
        recording_type="todo",
    )
    comment = EventRef(
        source="timeline",
        project_id=2222222,
        recording_id=3333333,
        parent_recording_id=4444444,
        recording_type="comment",
    )
    assert build_context_id(todo) == "item:2222222:4444444"
    assert build_context_id(comment) == build_context_id(todo)
    assert parse_context_id(build_context_id(comment)) == (
        "item",
        2222222,
        4444444,
    )


def test_chat_context_is_stable_per_room() -> None:
    event = EventRef(
        source="chat",
        project_id=2222222,
        room_id=5555555,
        recording_id=10220646952,
    )
    assert build_context_id(event) == "chat:2222222:5555555"
    assert parse_context_id(build_context_id(event)) == (
        "chat",
        2222222,
        5555555,
    )


def test_notification_with_room_routes_to_chat_context() -> None:
    event = EventRef(
        source="notification",
        event_id=1,
        project_id=10,
        room_id=20,
        recording_id=301,
        recording_type="chat",
    )

    assert build_context_id(event) == "chat:10:20"
    assert parse_context_id(build_context_id(event)) == (
        "chat",
        10,
        20,
    )


def test_ping_notification_routes_to_ping_context() -> None:
    event = EventRef(
        source="notification",
        event_id="9:revision:1",
        project_id=50,
        room_id=60,
        recording_id=70,
        recording_type="ping",
    )

    assert build_context_id(event) == "ping:50:60"
    assert parse_context_id(build_context_id(event)) == ("ping", 50, 60)


def test_snapshot_state_returns_only_new_foreign_events(tmp_path: Path) -> None:
    state = SnapshotState(tmp_path / "state.json", own_person_id=999)
    baseline = [
        EventRef(source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=123),
        EventRef(source="chat", event_id=2, project_id=10, room_id=20, recording_id=200, creator_id=999),
    ]
    assert state.update(baseline) == []

    changed = baseline + [
        EventRef(source="timeline", event_id=3, project_id=10, recording_id=300, creator_id=123),
        EventRef(source="chat", event_id=4, project_id=10, room_id=20, recording_id=400, creator_id=999),
    ]
    assert [event.event_id for event in state.update(changed)] == [3]
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["seen"] == ["chat:2", "chat:4", "timeline:1", "timeline:3"]


def test_invalid_context_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_context_id("todo:missing")
