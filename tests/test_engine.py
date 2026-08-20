from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from basecamp_platform.core import EventBatch, EventRef
from basecamp_platform.engine import DurableQueue, PollingEngine


class FakeClient:
    def __init__(
        self,
        snapshots: list[list[EventRef]],
        bucket_snapshots: list[set[str]] | None = None,
    ):
        self.snapshots = list(snapshots)
        self.bucket_snapshots = list(bucket_snapshots or [])
        self.boosted: list[int | str | None] = []

    def collect_events(self, **_kwargs) -> EventBatch:
        events = self.snapshots.pop(0)
        if self.bucket_snapshots:
            buckets = self.bucket_snapshots.pop(0)
        else:
            buckets = {"timeline"}
            buckets.update(
                f"chat:{event.project_id}:{event.room_id}"
                for event in events
                if event.source == "chat"
            )
        return EventBatch(events=events, buckets=buckets)

    def ensure_boost(self, event: EventRef, *, own_person_id: int, emoji: str):
        self.boosted.append(event.event_id)
        return {"status": "confirmed", "target": event.recording_id}


@pytest.mark.asyncio
async def test_run_blocking_waits_for_worker_before_finishing_cancellation() -> None:
    from basecamp_platform import engine as engine_module

    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        started.set()
        release.wait(timeout=5)

    task = asyncio.create_task(engine_module.run_blocking(worker))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def _engine(client, tmp_path, dispatch, authorize=None):
    return PollingEngine(
        client=client,
        state_path=tmp_path / "state.json",
        own_person_id=50,
        emoji="👀",
        dispatch=dispatch,
        authorize=authorize,
    )


@pytest.mark.asyncio
async def test_baseline_does_not_dispatch_existing_events(tmp_path: Path) -> None:
    old = EventRef(source="chat", event_id=1, project_id=10, room_id=20, recording_id=1, creator_id=40)
    client = FakeClient([[old]])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost: dict) -> None:
        dispatched.append(event.event_id)

    await _engine(client, tmp_path, dispatch).poll_once()

    assert dispatched == []
    assert client.boosted == []


@pytest.mark.asyncio
async def test_new_event_stays_pending_until_basecamp_delivery(tmp_path: Path) -> None:
    old = EventRef(source="chat", event_id=1, project_id=10, room_id=20, recording_id=1, creator_id=40)
    new = EventRef(source="chat", event_id=2, project_id=10, room_id=20, recording_id=2, creator_id=40)
    client = FakeClient([[old], [old, new]])
    order: list[str] = []
    original_boost = client.ensure_boost

    def boost(*args, **kwargs):
        order.append("boost")
        return original_boost(*args, **kwargs)

    client.ensure_boost = boost  # type: ignore[method-assign]

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        order.append("dispatch")
        assert context_id == "chat:10:20"
        assert boost_result["status"] == "confirmed"

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()
    await engine.poll_once()

    assert order == ["boost", "dispatch"]
    assert [item.event_id for item in engine.pending()] == [2]
    engine.complete_context("chat:10:20")
    assert engine.pending() == []


@pytest.mark.asyncio
async def test_failed_dispatch_stays_pending_and_retries(tmp_path: Path) -> None:
    old = EventRef(source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=40)
    new = EventRef(source="timeline", event_id=2, project_id=10, recording_id=200, creator_id=40)
    client = FakeClient([[old], [old, new], [old, new]])
    attempts = 0

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()
    with pytest.raises(RuntimeError, match="temporary"):
        await engine.poll_once()
    assert [item.event_id for item in engine.pending()] == [2]

    await engine.poll_once()
    assert attempts == 2
    engine.complete_context("item:10:200")
    assert engine.pending() == []


@pytest.mark.asyncio
async def test_own_events_are_seen_but_never_dispatched(tmp_path: Path) -> None:
    known = EventRef(source="chat", event_id=1, project_id=10, room_id=20, recording_id=1, creator_id=40)
    own = EventRef(source="chat", event_id=2, project_id=10, room_id=20, recording_id=2, creator_id=50)
    client = FakeClient([[known], [known, own]])
    dispatched: list[EventRef] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()
    await engine.poll_once()

    assert dispatched == []
    assert client.boosted == []


@pytest.mark.asyncio
async def test_unauthorized_event_never_persists_or_boosts(tmp_path: Path) -> None:
    # Establish the room bucket with an authorized event first, so the
    # outsider is not merely baselined away.
    insider = EventRef(source="chat", event_id=1, project_id=10, room_id=20, recording_id=1, creator_id=40)
    outsider = EventRef(source="chat", event_id=2, project_id=10, room_id=20, recording_id=2, creator_id=999)
    client = FakeClient([[insider], [insider, outsider]])
    dispatched: list[EventRef] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event)

    engine = _engine(client, tmp_path, dispatch, authorize=lambda e: e.creator_id == 40)
    await engine.poll_once()
    await engine.poll_once()

    assert dispatched == []
    assert client.boosted == []
    assert engine.pending() == []
    # The unauthorized event content must never reach the pending queue on disk.
    import json

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["pending"] == []


@pytest.mark.asyncio
async def test_revoked_pending_event_is_dropped_before_dispatch(tmp_path: Path) -> None:
    event = EventRef(
        source="notification",
        event_id="revoked:1",
        project_id=10,
        recording_id=200,
        creator_id=40,
        content="no longer authorized",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "seen": [event.identity],
                "buckets": ["notifications"],
                "pending": [event.to_dict()],
            }
        )
    )
    client = FakeClient([[event]], [{"notifications"}])
    dispatched: list[EventRef] = []

    async def dispatch(item: EventRef, _context: str, _boost: dict) -> None:
        dispatched.append(item)

    engine = PollingEngine(
        client=client,
        state_path=state_path,
        own_person_id=50,
        emoji="👀",
        dispatch=dispatch,
        authorize=lambda _event: False,
    )

    await engine.poll_once()

    assert dispatched == []
    assert client.boosted == []
    assert engine.pending() == []


@pytest.mark.asyncio
async def test_unboostable_event_still_reaches_agent(tmp_path: Path) -> None:
    known = EventRef(source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=40)
    event = EventRef(source="timeline", event_id=2, project_id=10, recording_id=200, creator_id=40)
    client = FakeClient([[known], [known, event]])

    def fail_boost(*args, **kwargs):
        raise RuntimeError("boosts unsupported")

    client.ensure_boost = fail_boost  # type: ignore[method-assign]
    received: list[dict] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        received.append(boost_result)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()
    await engine.poll_once()

    assert received == [{"status": "failed", "error": "boosts unsupported"}]
    engine.complete_context("item:10:200")
    assert engine.pending() == []


@pytest.mark.asyncio
async def test_scheduled_but_undelivered_event_survives_restart(tmp_path: Path) -> None:
    known = EventRef(source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=40)
    event = EventRef(source="timeline", event_id=2, project_id=10, recording_id=200, creator_id=40)
    first_client = FakeClient([[known], [known, event]])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event.event_id)

    first = _engine(first_client, tmp_path, dispatch)
    await first.poll_once()
    await first.poll_once()  # schedules work, but no Basecamp send confirmation
    assert [item.event_id for item in first.pending()] == [2]

    # New engine simulates a gateway process restart. In-memory inflight state
    # is gone, durable pending remains and is retried.
    second_client = FakeClient([[known, event]])
    second = _engine(second_client, tmp_path, dispatch)
    await second.poll_once()

    assert dispatched == [2, 2]
    assert [item.event_id for item in second.pending()] == [2]
    second.complete_context("item:10:200")
    assert second.pending() == []


def test_ingest_drops_legacy_unroutable_chat_aggregate(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    stale = EventRef(
        source="notification",
        event_id="legacy:1",
        project_id=10,
        room_id=None,
        recording_id=20,
        recording_type="chat",
        creator_id=40,
        content="old aggregate",
    )
    state_path.write_text(
        json.dumps(
            {
                "seen": [stale.identity],
                "buckets": ["notifications"],
                "pending": [stale.to_dict()],
            }
        )
    )
    queue = DurableQueue(state_path, own_person_id=50)

    queue.ingest([], lambda _event: True, {"notifications"})

    assert queue.pending() == []


def test_corrupt_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{broken")
    queue = DurableQueue(state_path, own_person_id=50)

    with pytest.raises(RuntimeError, match="state is unreadable"):
        queue.ingest([], lambda _event: True, {"notifications"})

    assert state_path.read_text() == "{broken"


def test_schema_corrupt_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    original = '{"seen": [], "buckets": [], "pending": "not-a-list"}'
    state_path.write_text(original)
    queue = DurableQueue(state_path, own_person_id=50)

    with pytest.raises(RuntimeError, match="state is unreadable"):
        queue.ingest([], lambda _event: True, {"notifications"})

    assert state_path.read_text() == original


def test_recording_v2_upgrade_drops_legacy_item_pending_but_keeps_chat(
    tmp_path: Path,
) -> None:
    item = EventRef(
        source="notification",
        event_id="legacy:revision:1",
        project_id=10,
        recording_id=201,
        parent_recording_id=200,
        recording_type="comment",
        creator_id=40,
    )
    chat = EventRef(
        source="notification",
        event_id="chat:10:20:line:301",
        project_id=10,
        room_id=20,
        recording_id=301,
        recording_type="chat",
        creator_id=40,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "seen": [item.identity, chat.identity],
                "buckets": ["notifications", "chat-lines-v2", "ping-lines-v2"],
                "pending": [item.to_dict(), chat.to_dict()],
            }
        )
    )
    queue = DurableQueue(state_path, own_person_id=50)
    current = EventRef(
        source="notification",
        event_id="recording:10:202",
        project_id=10,
        recording_id=202,
        parent_recording_id=200,
        recording_type="comment",
        creator_id=40,
    )

    queue.ingest(
        [current],
        lambda _event: True,
        {"notifications", "recording-notifications-v2"},
    )

    assert [event.identity for event in queue.pending()] == [chat.identity]
    assert current.identity in queue.watermarks()[0]

    restarted = DurableQueue(state_path, own_person_id=50)
    restarted.ingest(
        [current],
        lambda _event: True,
        {"notifications", "recording-notifications-v2"},
    )
    assert [event.identity for event in restarted.pending()] == [chat.identity]


@pytest.mark.asyncio
async def test_newly_discovered_room_is_baselined_not_replayed(tmp_path: Path) -> None:
    # First poll only sees the timeline bucket. A room discovered later must be
    # baselined, never replayed retroactively.
    tl = EventRef(source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=40)
    historical = EventRef(source="chat", event_id=2, project_id=10, room_id=20, recording_id=2, creator_id=40)
    fresh = EventRef(source="chat", event_id=3, project_id=10, room_id=20, recording_id=3, creator_id=40)
    client = FakeClient([[tl], [tl, historical], [tl, historical, fresh]])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event.event_id)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()  # baseline timeline
    await engine.poll_once()  # discover room 20 → baseline, do NOT replay historical
    assert dispatched == []
    await engine.poll_once()  # genuinely new message in known room → dispatch
    assert dispatched == [3]


def test_recency_trim_keeps_latest_not_lexicographically_largest() -> None:
    queue = DurableQueue(Path("/tmp/unused-state.json"), own_person_id=50, max_seen=2)
    trimmed = queue._recency_trim(
        ["timeline:900", "timeline:1000", "timeline:1001"], cap=2
    )
    # Recency order preserved; lexical sort would have dropped 1000/1001.
    assert trimmed == ["timeline:1000", "timeline:1001"]


@pytest.mark.asyncio
async def test_high_numeric_id_is_not_reboosted_after_seen_cap(tmp_path: Path) -> None:
    known = EventRef(source="timeline", event_id=900, project_id=10, recording_id=900, creator_id=40)
    big = EventRef(source="timeline", event_id=1000, project_id=10, recording_id=1000, creator_id=40)
    client = FakeClient([[known], [known, big], [known, big]])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event.event_id)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()  # baseline
    await engine.poll_once()  # dispatch 1000 once
    await engine.poll_once()  # must NOT re-dispatch 1000

    assert dispatched == [1000]
    assert client.boosted == [1000]


@pytest.mark.asyncio
async def test_first_future_event_after_empty_timeline_is_dispatched(tmp_path: Path) -> None:
    first = EventRef(
        source="timeline", event_id=1, project_id=10, recording_id=100, creator_id=40
    )
    client = FakeClient([[], [first]])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event.event_id)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()  # empty timeline is nevertheless recorded as known
    await engine.poll_once()

    assert dispatched == [1]


@pytest.mark.asyncio
async def test_first_future_event_after_empty_room_is_dispatched(tmp_path: Path) -> None:
    first = EventRef(
        source="chat",
        event_id=1,
        project_id=10,
        room_id=20,
        recording_id=100,
        creator_id=40,
    )
    buckets = {"timeline", "chat:10:20"}
    client = FakeClient([[], [first]], bucket_snapshots=[buckets, buckets])
    dispatched: list[int | str | None] = []

    async def dispatch(event: EventRef, context_id: str, boost_result: dict) -> None:
        dispatched.append(event.event_id)

    engine = _engine(client, tmp_path, dispatch)
    await engine.poll_once()  # empty room is explicitly recorded as known
    await engine.poll_once()

    assert dispatched == [1]
