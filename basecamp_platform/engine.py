"""Durable polling engine for Basecamp gateway events."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from .core import EventBatch, EventRef, build_context_id

logger = logging.getLogger(__name__)


class Client(Protocol):
    def collect_events(
        self,
        *,
        seen_identities: set[str] | None = None,
        known_buckets: set[str] | None = None,
        own_person_id: int | None = None,
    ) -> EventBatch: ...

    def ensure_boost(
        self,
        event: EventRef,
        *,
        own_person_id: int,
        emoji: str,
    ) -> dict[str, Any]: ...


Dispatch = Callable[[EventRef, str, dict[str, Any]], Awaitable[None]]
Authorize = Callable[[EventRef], bool]


async def run_blocking(func, /, *args, **kwargs):
    """Run blocking I/O while waiting for an already-started job on cancel."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, partial(func, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await future
        except Exception as exc:  # noqa: BLE001 - cancellation remains authoritative
            logger.debug("Blocking worker failed during cancellation: %s", exc)
        raise


def source_bucket(event: EventRef) -> str:
    """Coarse discovery key: one bucket per timeline and per Campfire room.

    Baseline is tracked per source so a newly discovered project or room is
    seeded without retroactively acting on its historical items.
    """
    if event.source == "notification":
        return "notifications"
    if event.source == "chat":
        return f"chat:{event.project_id}:{event.room_id}"
    return "timeline"


class DurableQueue:
    def __init__(self, path: Path, own_person_id: int, max_seen: int = 10_000):
        self.path = Path(path)
        self.own_person_id = own_person_id
        self.max_seen = max(100, int(max_seen))

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"seen": [], "buckets": [], "pending": []}
        except OSError:
            raise RuntimeError("Basecamp state is unreadable; refusing overwrite") from None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("Basecamp state is unreadable; refusing overwrite") from None
        if not isinstance(value, dict):
            raise RuntimeError(  # noqa: TRY004 - persistent state contract
                "Basecamp state is unreadable; refusing overwrite"
            )
        if any(not isinstance(value.get(key, []), list) for key in ("seen", "buckets", "pending")):
            raise RuntimeError("Basecamp state is unreadable; refusing overwrite")
        if any(not isinstance(item, dict) for item in value.get("pending", [])):
            raise RuntimeError("Basecamp state is unreadable; refusing overwrite")
        value.setdefault("seen", [])
        value.setdefault("buckets", [])
        value.setdefault("pending", [])
        return value

    @staticmethod
    def _recency_trim(seen: list[str], cap: int) -> list[str]:
        """Keep the most recently observed identities, preserving order."""
        deduped: list[str] = []
        collapsed = set()
        for identity in reversed(seen):
            if identity in collapsed:
                continue
            collapsed.add(identity)
            deduped.append(identity)
        deduped.reverse()
        return deduped[-cap:]

    def _save(self, seen: list[str], buckets: list[str] | set[str], pending: list[dict[str, Any]]) -> None:
        payload = {
            "seen": self._recency_trim(seen, self.max_seen),
            "buckets": sorted(set(buckets)),
            "pending": pending,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def ingest(
        self,
        events: list[EventRef],
        authorize: Authorize,
        discovered_buckets: set[str] | None = None,
        discovered_seen: set[str] | None = None,
    ) -> None:
        value = self._load()
        seen = [str(item) for item in value.get("seen", [])]
        seen_set = set(seen)
        for identity in discovered_seen or set():
            if identity not in seen_set:
                seen.append(identity)
                seen_set.add(identity)
        known_buckets = {str(item) for item in value.get("buckets", [])}
        recording_marker = "recording-notifications-v2"
        recording_upgrade = (
            recording_marker in (discovered_buckets or set())
            and recording_marker not in known_buckets
        )
        comment_marker = "comment-recordings-v1"
        comment_upgrade = (
            comment_marker in (discovered_buckets or set())
            and comment_marker not in known_buckets
        )
        pending: list[EventRef] = []
        for item in value.get("pending", []):
            if not isinstance(item, dict):
                continue
            event = EventRef.from_dict(item)
            if (event.recording_type or "").lower() == "chat" and not event.room_id:
                # Migration cleanup for pre-line-resolution transcript
                # aggregates, which cannot be replied to as work items.
                continue
            pending.append(event)
        if recording_upgrade:
            pending = [
                event
                for event in pending
                if (event.recording_type or "").lower() in {"chat", "ping"}
            ]
        pending_ids = {item.identity for item in pending}

        current_buckets = {source_bucket(event) for event in events}
        current_buckets.update(discovered_buckets or set())
        new_buckets = current_buckets - known_buckets

        for event in events:
            identity = event.identity
            already_seen = identity in seen_set
            if identity not in seen_set:
                seen.append(identity)
                seen_set.add(identity)
            if recording_upgrade and (
                event.recording_type or ""
            ).lower() not in {"chat", "ping"}:
                continue
            if comment_upgrade and (event.recording_type or "").lower() == "comment":
                continue
            # A source seen for the first time is baselined, never dispatched.
            if source_bucket(event) in new_buckets:
                continue
            if already_seen or identity in pending_ids:
                continue
            if event.creator_id == self.own_person_id:
                continue
            # Authorize BEFORE persisting any event content to the queue.
            if not authorize(event):
                continue
            pending.append(event)
            pending_ids.add(identity)

        self._save(
            seen,
            known_buckets | current_buckets,
            [event.to_dict() for event in pending],
        )

    def pending(self) -> list[EventRef]:
        value = self._load()
        return [
            EventRef.from_dict(item)
            for item in value.get("pending", [])
            if isinstance(item, dict)
        ]

    def watermarks(self) -> tuple[set[str], set[str]]:
        value = self._load()
        return (
            {str(item) for item in value.get("seen", [])},
            {str(item) for item in value.get("buckets", [])},
        )

    def complete(self, identity: str) -> None:
        value = self._load()
        pending = [
            item
            for item in value.get("pending", [])
            if isinstance(item, dict)
            and EventRef.from_dict(item).identity != identity
        ]
        self._save(
            [str(item) for item in value.get("seen", [])],
            [str(item) for item in value.get("buckets", [])],
            pending,
        )

    def complete_context(self, context_id: str) -> list[str]:
        """A successful Basecamp reply acknowledges all pending work for context."""
        value = self._load()
        completed: list[str] = []
        retained: list[dict[str, Any]] = []
        for item in value.get("pending", []):
            if not isinstance(item, dict):
                continue
            event = EventRef.from_dict(item)
            try:
                same_context = build_context_id(event) == context_id
            except ValueError:
                same_context = False
            if same_context:
                completed.append(event.identity)
            else:
                retained.append(item)
        self._save(
            [str(item) for item in value.get("seen", [])],
            [str(item) for item in value.get("buckets", [])],
            retained,
        )
        return completed


class PollingEngine:
    def __init__(
        self,
        *,
        client: Client,
        state_path: Path,
        own_person_id: int,
        emoji: str,
        dispatch: Dispatch,
        authorize: Authorize | None = None,
    ):
        self.client = client
        self.own_person_id = own_person_id
        self.emoji = emoji
        self.dispatch = dispatch
        self.authorize = authorize or (lambda _event: True)
        self.queue = DurableQueue(state_path, own_person_id)
        self._inflight: set[str] = set()

    def pending(self) -> list[EventRef]:
        return self.queue.pending()

    def complete_context(self, context_id: str) -> None:
        completed = self.queue.complete_context(context_id)
        self._inflight.difference_update(completed)

    def complete(self, identity: str) -> None:
        self.queue.complete(identity)
        self._inflight.discard(identity)

    def retry(self, identity: str) -> None:
        self._inflight.discard(identity)

    async def poll_once(self) -> None:
        seen, buckets = self.queue.watermarks()
        batch = await run_blocking(
            self.client.collect_events,
            seen_identities=seen,
            known_buckets=buckets,
            own_person_id=self.own_person_id,
        )
        self.queue.ingest(
            batch.events,
            self.authorize,
            batch.buckets,
            batch.watermarks,
        )
        for event in self.queue.pending():
            if event.creator_id == self.own_person_id or not self.authorize(event):
                self.complete(event.identity)
                continue
            if event.identity in self._inflight:
                continue
            self._inflight.add(event.identity)
            try:
                boost = await run_blocking(
                    self.client.ensure_boost,
                    event,
                    own_person_id=self.own_person_id,
                    emoji=self.emoji,
                )
            except RuntimeError as exc:
                boost = {"status": "failed", "error": str(exc)}
            try:
                await self.dispatch(event, build_context_id(event), boost)
            except Exception:
                self._inflight.discard(event.identity)
                raise
