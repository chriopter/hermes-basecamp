"""Pure Basecamp event and state primitives."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def strict_bool(value: Any) -> bool:
    """Fail-closed boolean coercion for YAML/user config values."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


@dataclass(slots=True)
class EventRef:
    source: str
    event_id: int | str | None = None
    project_id: int | None = None
    room_id: int | None = None
    recording_id: int | None = None
    parent_recording_id: int | None = None
    recording_type: str | None = None
    creator_id: int | None = None
    creator_name: str | None = None
    content: str = ""
    app_url: str | None = None
    created_at: str | None = None
    kind: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.source}:{self.event_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EventRef:
        return cls(
            source=str(value.get("source") or ""),
            event_id=value.get("event_id"),
            project_id=value.get("project_id"),
            room_id=value.get("room_id"),
            recording_id=value.get("recording_id"),
            parent_recording_id=value.get("parent_recording_id"),
            recording_type=value.get("recording_type"),
            creator_id=value.get("creator_id"),
            creator_name=value.get("creator_name"),
            content=str(value.get("content") or ""),
            app_url=value.get("app_url"),
            created_at=value.get("created_at"),
            kind=value.get("kind"),
        )


@dataclass(slots=True)
class EventBatch:
    events: list[EventRef]
    buckets: set[str]


def recording_id_from_url(url: str | None) -> int | None:
    path = urlparse(str(url or "")).path
    match = re.search(r"/(\d+)(?:\.json)?/?$", path)
    return int(match.group(1)) if match else None


def build_context_id(event: EventRef) -> str:
    if not event.project_id:
        raise ValueError("Basecamp event has no project ID")
    if event.room_id:
        if not event.room_id:
            raise ValueError("Basecamp chat event has no room ID")
        if (event.recording_type or "").lower() == "ping":
            return f"ping:{event.project_id}:{event.room_id}"
        return f"chat:{event.project_id}:{event.room_id}"
    if (event.recording_type or "").lower() == "comment":
        root_id = event.parent_recording_id or event.recording_id
    else:
        root_id = event.recording_id or event.parent_recording_id
    if not root_id:
        raise ValueError("Basecamp item event has no recording ID")
    return f"item:{event.project_id}:{root_id}"


def parse_context_id(value: str) -> tuple[str, int, int]:
    match = re.fullmatch(r"(chat|item|ping):(\d+):(\d+)", value or "")
    if not match:
        raise ValueError(f"Invalid Basecamp context ID: {value!r}")
    return match.group(1), int(match.group(2)), int(match.group(3))


class SnapshotState:
    """Persist IDs and return only new non-self events."""

    def __init__(self, path: Path, own_person_id: int | None, max_seen: int = 10_000):
        self.path = Path(path)
        self.own_person_id = own_person_id
        self.max_seen = max(100, int(max_seen))

    def _load(self) -> tuple[bool, set[str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False, set()
        return True, {str(item) for item in value.get("seen", [])}

    def _save(self, seen: Iterable[str]) -> None:
        ordered = sorted(set(seen))[-self.max_seen :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"seen": ordered}, sort_keys=True), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def update(self, events: Iterable[EventRef]) -> list[EventRef]:
        existed, seen = self._load()
        events = list(events)
        new_events = [
            event
            for event in events
            if existed
            and event.identity not in seen
            and event.creator_id != self.own_person_id
        ]
        seen.update(event.identity for event in events)
        self._save(seen)
        return new_events
