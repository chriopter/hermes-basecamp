from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import build_session_key
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

from basecamp_platform.adapter import BasecampAdapter
from basecamp_platform.core import EventBatch, EventRef


class FakeCLI:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.added_boosts: list[tuple[int, str, str]] = []
        self.deleted_boosts: list[tuple[int, str]] = []

    def current_person_id(self) -> int:
        return 50

    def collect_events(self, **_kwargs) -> EventBatch:
        return EventBatch(events=[], buckets={"timeline"})

    def ensure_boost(
        self, event: EventRef, *, own_person_id: int, emoji: str
    ) -> dict:
        return {"status": "confirmed"}

    def reply(self, context_id: str, text: str):
        self.replies.append((context_id, text))
        return {"ok": True, "data": {"id": 99}}

    def edit_reply(self, context_id: str, message_id: str, text: str):
        self.edits.append((context_id, message_id, text))
        return {"id": int(message_id)}

    def add_boost(self, bucket_id: int, recording_id: str, emoji: str) -> str:
        self.added_boosts.append((bucket_id, recording_id, emoji))
        return f"boost-{len(self.added_boosts)}"

    def delete_boost(self, bucket_id: int, boost_id: str) -> None:
        self.deleted_boosts.append((bucket_id, boost_id))


@pytest.fixture
def adapter(tmp_path: Path) -> BasecampAdapter:
    config = SimpleNamespace(
        extra={
            "poll_interval_seconds": 10,
            "acknowledgement_emoji": "👀",
            "own_person_id": 50,
        }
    )
    return BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )


@pytest.mark.asyncio
async def test_dispatch_builds_native_message_event_with_stable_context(adapter: BasecampAdapter) -> None:
    captured = []

    async def handle(event):
        captured.append(event)

    adapter.handle_message = handle  # type: ignore[method-assign]
    event = EventRef(
        source="timeline",
        event_id=30,
        project_id=10,
        recording_id=300,
        parent_recording_id=200,
        recording_type="comment",
        creator_id=40,
        creator_name="Chris",
        content="@Agent please check",
        kind="comment_created",
        app_url="https://app.basecamp.test/item/300",
        created_at="2026-08-20T09:00:00Z",
    )

    await adapter._dispatch(event, "item:10:200", {"status": "confirmed"})

    assert len(captured) == 1
    message = captured[0]
    assert message.source.chat_id == "item:10:200"
    assert message.source.user_id == "40"
    assert message.source.scope_id == "10"
    assert message.source.message_id == "timeline:30"
    assert message.message_id == "timeline:30"
    assert message.timestamp.isoformat() == "2026-08-20T09:00:00+00:00"
    assert message.allow_gateway_control is False
    assert "@Agent please check" in message.text
    assert "confirmed" in message.text


@pytest.mark.asyncio
async def test_send_routes_gateway_response_back_to_basecamp(adapter: BasecampAdapter) -> None:
    result = await adapter.send("item:10:200", "Done")

    assert result.success is True
    assert result.message_id == "99"
    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.replies == [("item:10:200", "Done")]


@pytest.mark.asyncio
async def test_send_extracts_direct_api_message_id(adapter: BasecampAdapter) -> None:
    assert isinstance(adapter.cli, FakeCLI)
    adapter.cli.reply = lambda _context, _text: {"id": 123}  # type: ignore[method-assign]

    result = await adapter.send("ping:10:20", "Done")

    assert result.success is True
    assert result.message_id == "123"


@pytest.mark.asyncio
async def test_stream_edits_one_reply_and_swaps_pencil_for_check(
    adapter: BasecampAdapter,
) -> None:
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": ["notification:1"]}},
    )
    await adapter.on_processing_start(event)

    first = await adapter.edit_message(
        "item:10:200", "99", "Draft", finalize=False
    )
    second = await adapter.edit_message(
        "item:10:200", "99", "Longer draft", finalize=False
    )
    final = await adapter.edit_message(
        "item:10:200", "99", "Final answer", finalize=True
    )

    assert first.success and second.success and final.success
    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.edits == [
        ("item:10:200", "99", "Draft"),
        ("item:10:200", "99", "Longer draft"),
        ("item:10:200", "99", "Final answer"),
    ]
    assert adapter.cli.added_boosts == [(10, "99", "✏️")]

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert adapter.cli.added_boosts == [
        (10, "99", "✏️"),
        (10, "99", "✅"),
    ]
    assert adapter.cli.deleted_boosts == [(10, "boost-1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [ProcessingOutcome.SUCCESS, ProcessingOutcome.FAILURE, ProcessingOutcome.CANCELLED],
)
async def test_processing_completion_removes_input_eyes_boost(
    adapter: BasecampAdapter, outcome: ProcessingOutcome
) -> None:
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={
            "basecamp": {
                "project_id": 10,
                "delivery_ids": ["notification:eyes"],
                "acknowledgement": {"boost_id": "eyes-1"},
            }
        },
    )

    await adapter.on_processing_complete(event, outcome)

    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.deleted_boosts == [(10, "eyes-1")]


@pytest.mark.asyncio
async def test_stream_status_delete_failure_keeps_tracking_until_retry(
    adapter: BasecampAdapter,
) -> None:
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": ["notification:retry"]}},
    )
    await adapter.on_processing_start(event)
    await adapter.edit_message(
        source.chat_id, "99", "Partial", finalize=False
    )
    assert isinstance(adapter.cli, FakeCLI)
    original_delete = adapter.cli.delete_boost
    attempts = 0

    def flaky_delete(bucket_id: int, boost_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary delete failure")
        original_delete(bucket_id, boost_id)

    adapter.cli.delete_boost = flaky_delete  # type: ignore[method-assign]

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert adapter.cli.added_boosts == [(10, "99", "✏️")]
    assert ("notification:retry",) in adapter._stream_statuses
    await adapter._flush_stream_status_retries()
    assert adapter.cli.added_boosts == [
        (10, "99", "✏️"),
        (10, "99", "✅"),
    ]
    assert adapter._stream_statuses == {}


@pytest.mark.asyncio
async def test_gateway_stream_consumer_edits_one_basecamp_response(
    adapter: BasecampAdapter,
) -> None:
    identity = "notification:stream:gateway"
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": [identity]}},
    )
    await adapter.on_processing_start(event)
    consumer = GatewayStreamConsumer(
        adapter,
        source.chat_id,
        StreamConsumerConfig(
            edit_interval=0.01,
            buffer_threshold=1,
            cursor=" ▉",
            transport="edit",
        ),
    )
    task = asyncio.create_task(consumer.run())

    consumer.on_delta("Hello")
    await asyncio.sleep(0.03)
    consumer.on_delta(" world")
    await asyncio.sleep(0.03)
    consumer.finish()
    await task
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert isinstance(adapter.cli, FakeCLI)
    assert len(adapter.cli.replies) == 1
    assert adapter.cli.edits
    assert all(message_id == "99" for _, message_id, _ in adapter.cli.edits)
    assert adapter.cli.edits[-1] == (
        "item:10:200",
        "99",
        "Hello world",
    )
    assert adapter.cli.added_boosts == [
        (10, "99", "✏️"),
        (10, "99", "✅"),
    ]


@pytest.mark.asyncio
async def test_send_appends_real_session_id_when_debug_footer_enabled(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        extra={
            "own_person_id": 50,
            "allow_from": ["40"],
            "debug_session_footer": True,
        }
    )
    adapter = BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    class Store:
        def peek_session_id(self, session_key: str) -> str:
            assert session_key
            return "20260820_120000_deadbe"

    adapter.set_session_store(Store())
    event = EventRef(
        source="timeline",
        event_id=1,
        project_id=10,
        recording_id=200,
        creator_id=40,
    )

    async def handle_message(_message):
        return None

    adapter.handle_message = handle_message  # type: ignore[method-assign]
    await adapter._dispatch(event, "item:10:200", {})
    result = await adapter.send("item:10:200", "Done")

    assert result.success is True
    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.replies == [
        (
            "item:10:200",
            "Done\n\nDebug: Hermes-Session 20260820_120000_deadbe",
        )
    ]


@pytest.mark.asyncio
async def test_send_does_not_retry_ambiguous_timeout(
    adapter: BasecampAdapter,
) -> None:
    assert isinstance(adapter.cli, FakeCLI)

    def timeout(context_id: str, text: str):
        raise RuntimeError("basecamp chat post timed out")

    adapter.cli.reply = timeout  # type: ignore[method-assign]

    result = await adapter.send("chat:10:20", "SENSITIVE_REPLY")

    assert result.success is False
    assert result.retryable is False
    assert result.error == "basecamp chat post timed out"
    assert "SENSITIVE_REPLY" not in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        ("basecamp comments create failed (exit 4)", False),
        ("Basecamp API request failed", False),
        ("Basecamp API request timed out", False),
        ("Basecamp API request failed (HTTP 503)", False),
        ("Basecamp API request failed (HTTP 429)", True),
        ("Basecamp API request failed (HTTP 401)", False),
    ],
)
async def test_send_classifies_retryability(
    adapter: BasecampAdapter, error: str, retryable: bool
) -> None:
    assert isinstance(adapter.cli, FakeCLI)

    def fail(_context_id: str, _text: str):
        raise RuntimeError(error)

    adapter.cli.reply = fail  # type: ignore[method-assign]

    result = await adapter.send("item:10:200", "reply")

    assert result.success is False
    assert result.retryable is retryable


@pytest.mark.asyncio
async def test_send_normalizes_unexpected_transport_exception(
    adapter: BasecampAdapter,
) -> None:
    assert isinstance(adapter.cli, FakeCLI)
    adapter.cli.reply = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ValueError("unexpected")
    )

    result = await adapter.send("item:10:200", "reply")

    assert result.success is False
    assert result.retryable is False
    assert result.error == "Basecamp send failed"


@pytest.mark.asyncio
async def test_confirmed_remote_send_survives_local_completion_failure(
    adapter: BasecampAdapter,
) -> None:
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": ["notification:1"]}},
    )
    await adapter.on_processing_start(event)
    adapter.engine.complete = lambda _identity: (_ for _ in ()).throw(  # type: ignore[method-assign]
        OSError("disk full")
    )

    result = await adapter.send("item:10:200", "reply")
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert result.success is True
    assert adapter._completion_retry_ids == {"notification:1"}


@pytest.mark.asyncio
async def test_send_defers_durable_ack_until_processing_success(
    adapter: BasecampAdapter,
) -> None:
    identity = "notification:stream:1"
    completed: list[str] = []
    adapter.engine.complete = completed.append  # type: ignore[method-assign]
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": [identity]}},
    )
    await adapter.on_processing_start(event)

    send_result = await adapter.send(source.chat_id, "First preview")

    assert send_result.success is True
    assert completed == []
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert completed == [identity]


@pytest.mark.asyncio
async def test_get_chat_info_describes_item_and_chat_contexts(adapter: BasecampAdapter) -> None:
    assert await adapter.get_chat_info("item:10:200") == {
        "chat_id": "item:10:200",
        "name": "Basecamp item 200",
        "type": "group",
    }
    assert await adapter.get_chat_info("chat:10:20") == {
        "chat_id": "chat:10:20",
        "name": "Basecamp chat 20",
        "type": "group",
    }
    assert await adapter.get_chat_info("ping:10:20") == {
        "chat_id": "ping:10:20",
        "name": "Basecamp ping 20",
        "type": "group",
    }


@pytest.mark.asyncio
async def test_disconnect_cancels_poll_task_without_nonexistent_base_shutdown(
    adapter: BasecampAdapter,
) -> None:
    adapter._poll_task = asyncio.create_task(asyncio.sleep(60))

    await adapter.disconnect()

    assert adapter._poll_task is None


@pytest.mark.asyncio
async def test_disconnect_stops_poller_before_releasing_credential_lock(
    adapter: BasecampAdapter,
) -> None:
    order: list[str] = []

    async def poller() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            order.append("poll-stopped")

    adapter._poll_task = asyncio.create_task(poller())
    await asyncio.sleep(0)
    adapter._lock_acquired = True
    adapter._release_credential_lock = lambda: order.append("lock-released")  # type: ignore[method-assign]

    await adapter.disconnect()

    assert order == ["poll-stopped", "lock-released"]


@pytest.mark.asyncio
async def test_poll_loop_polls_before_waiting(adapter: BasecampAdapter, monkeypatch) -> None:
    order: list[str] = []

    async def poll_once() -> None:
        order.append("poll")
        raise asyncio.CancelledError

    async def sleep(_seconds: float) -> None:
        order.append("sleep")

    adapter.engine.poll_once = poll_once  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._poll_loop()

    assert order == ["poll"]


@pytest.mark.asyncio
async def test_repeated_poll_failures_transition_to_retryable_fatal(
    adapter: BasecampAdapter, monkeypatch
) -> None:
    attempts = 0
    notified = False
    adapter.poll_failure_threshold = 2

    async def fail_poll() -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 3:
            raise asyncio.CancelledError
        raise RuntimeError("temporary outage")

    async def no_sleep(_seconds: float) -> None:
        return None

    async def notify() -> None:
        nonlocal notified
        notified = True

    adapter.engine.poll_once = fail_poll  # type: ignore[method-assign]
    adapter._notify_fatal_error = notify  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await adapter._poll_loop()

    assert attempts == 2
    assert adapter.fatal_error_code == "poll_failed"
    assert adapter.fatal_error_retryable is True
    assert notified is True


@pytest.mark.asyncio
async def test_successful_send_completes_only_one_pending_event_per_context(
    adapter: BasecampAdapter,
) -> None:
    first = EventRef(
        source="notification",
        event_id="chat:10:20:line:1",
        project_id=10,
        room_id=20,
        recording_id=1,
        recording_type="chat",
        creator_id=40,
        content="one",
    )
    second = EventRef(
        source="notification",
        event_id="chat:10:20:line:2",
        project_id=10,
        room_id=20,
        recording_id=2,
        recording_type="chat",
        creator_id=40,
        content="two",
    )
    adapter.engine.queue.ingest([], lambda _event: True, {"notifications"})
    adapter.engine.queue.ingest(
        [first, second], lambda _event: True, {"notifications"}
    )

    captured: list[MessageEvent] = []

    async def handle_message(message):
        captured.append(message)

    adapter.handle_message = handle_message  # type: ignore[method-assign]
    await adapter._dispatch(first, "chat:10:20", {})
    await adapter._dispatch(second, "chat:10:20", {})
    await adapter.on_processing_start(captured[0])

    result = await adapter.send("chat:10:20", "first response")
    await adapter.on_processing_complete(
        captured[0], ProcessingOutcome.SUCCESS
    )

    assert result.success is True
    assert [event.identity for event in adapter.engine.pending()] == [
        second.identity
    ]


@pytest.mark.asyncio
async def test_merged_busy_followup_completes_all_merged_delivery_ids(
    adapter: BasecampAdapter,
) -> None:
    events = [
        EventRef(
            source="notification",
            event_id=f"chat:10:20:line:{line_id}",
            project_id=10,
            room_id=20,
            recording_id=line_id,
            recording_type="chat",
            creator_id=40,
            content=str(line_id),
        )
        for line_id in (2, 3)
    ]
    adapter.engine.queue.ingest([], lambda _event: True, {"notifications"})
    adapter.engine.queue.ingest(events, lambda _event: True, {"notifications"})
    source = adapter.build_source(
        chat_id="chat:10:20",
        chat_type="group",
        user_id="40",
    )

    def message(event: EventRef) -> MessageEvent:
        return MessageEvent(
            text=event.content,
            message_type=MessageType.TEXT,
            source=source,
            message_id=event.identity,
            metadata={
                "basecamp": {
                    "project_id": 10,
                    "delivery_ids": [event.identity],
                    "acknowledgement": {
                        "boost_id": f"eyes-{event.recording_id}"
                    },
                }
            },
            allow_gateway_control=False,
        )

    queued = message(events[0])
    incoming = message(events[1])
    session_key = build_session_key(
        source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._pending_messages[session_key] = queued

    await adapter.handle_message(incoming)
    merged = adapter._pending_messages[session_key]
    await adapter.on_processing_start(merged)
    result = await adapter.send("chat:10:20", "merged response")
    await adapter.on_processing_complete(merged, ProcessingOutcome.SUCCESS)

    assert result.success is True
    assert merged.metadata["basecamp"]["delivery_ids"] == [
        events[0].identity,
        events[1].identity,
    ]
    assert adapter.engine.pending() == []
    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.deleted_boosts == [(10, "eyes-2"), (10, "eyes-3")]


@pytest.mark.asyncio
async def test_queue_debounce_does_not_merge_delivery_ids_across_senders(
    adapter: BasecampAdapter,
) -> None:
    adapter._busy_text_mode = "queue"

    async def handler(_event):
        return "unused"

    adapter.set_message_handler(handler)
    first_source = adapter.build_source(
        chat_id="chat:10:20", chat_type="group", user_id="40"
    )
    second_source = adapter.build_source(
        chat_id="chat:10:20", chat_type="group", user_id="41"
    )

    def message(source, identity):
        return MessageEvent(
            text=identity,
            message_type=MessageType.TEXT,
            source=source,
            message_id=identity,
            metadata={"basecamp": {"delivery_ids": [identity]}},
            allow_gateway_control=False,
        )

    first = message(first_source, "notification:first")
    second = message(second_source, "notification:second")
    session_key = build_session_key(
        first_source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(first)
    await adapter.handle_message(second)

    pending = adapter._pending_messages.get(session_key)
    assert pending is not None
    assert pending.metadata["basecamp"]["delivery_ids"] == [
        "notification:first"
    ]
    debounce = adapter._text_debounce_store().get(session_key)
    assert debounce is not None
    assert debounce.event.metadata["basecamp"]["delivery_ids"] == [
        "notification:second"
    ]


@pytest.mark.asyncio
async def test_per_user_sessions_keep_delivery_ids_task_local(
    adapter: BasecampAdapter,
) -> None:
    adapter.config.extra["group_sessions_per_user"] = True
    completed: list[str] = []
    adapter.engine.complete = completed.append  # type: ignore[method-assign]
    both_ready = asyncio.Event()
    ready = 0

    async def turn(user_id: str, identity: str) -> None:
        nonlocal ready
        source = adapter.build_source(
            chat_id="chat:10:20", chat_type="group", user_id=user_id
        )
        event = MessageEvent(
            text=identity,
            message_type=MessageType.TEXT,
            source=source,
            metadata={"basecamp": {"delivery_ids": [identity]}},
        )
        await adapter.on_processing_start(event)
        ready += 1
        if ready == 2:
            both_ready.set()
        await both_ready.wait()
        await adapter.send(source.chat_id, f"reply-{identity}")
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    await asyncio.gather(
        turn("40", "notification:first"),
        turn("41", "notification:second"),
    )

    assert sorted(completed) == ["notification:first", "notification:second"]


@pytest.mark.asyncio
async def test_failed_processing_releases_inflight_for_retry(
    adapter: BasecampAdapter,
) -> None:
    identity = "notification:chat:10:20:line:9"
    source = adapter.build_source(chat_id="chat:10:20", chat_type="group")
    event = MessageEvent(
        text="failure",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": [identity]}},
    )
    adapter.engine._inflight.add(identity)
    adapter._delivery_context.set((source.chat_id, [identity]))

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert identity not in adapter.engine._inflight


@pytest.mark.asyncio
async def test_failed_stream_swaps_pencil_for_cross_and_releases_delivery(
    adapter: BasecampAdapter,
) -> None:
    identity = "notification:stream:failure"
    source = adapter.build_source(chat_id="item:10:200", chat_type="group")
    event = MessageEvent(
        text="stream",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"basecamp": {"delivery_ids": [identity]}},
    )
    adapter.engine._inflight.add(identity)
    await adapter.on_processing_start(event)
    edit_result = await adapter.edit_message(
        source.chat_id, "99", "Partial answer", finalize=False
    )
    final_result = await adapter.edit_message(
        source.chat_id, "99", "Complete answer", finalize=True
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert edit_result.success is True and final_result.success is True
    assert identity not in adapter.engine._inflight
    assert isinstance(adapter.cli, FakeCLI)
    assert adapter.cli.added_boosts == [
        (10, "99", "✏️"),
        (10, "99", "❌"),
    ]
    assert adapter.cli.deleted_boosts == [(10, "boost-1")]


def test_items_share_one_session_across_basecamp_users(adapter: BasecampAdapter) -> None:
    assert adapter.config.extra["group_sessions_per_user"] is False


def test_early_authorization_uses_profile_scoped_allowlist(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BASECAMP_ALLOWED_USERS", "999")
    monkeypatch.setenv("BASECAMP_ALLOW_ALL_USERS", "false")
    config = SimpleNamespace(
        extra={
            "allow_from": ["40"],
            "group_allow_from": ["40"],
            "allow_all_users": False,
            "own_person_id": 50,
        }
    )
    scoped = BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    assert scoped._is_early_authorized(
        EventRef(source="chat", creator_id=40)
    ) is True
    assert scoped._is_early_authorized(
        EventRef(source="chat", creator_id=999)
    ) is False


def test_adapter_quoted_false_does_not_allow_all(tmp_path: Path) -> None:
    config = SimpleNamespace(
        extra={
            "allow_from": ["40"],
            "allow_all_users": "false",
            "own_person_id": 50,
        }
    )
    adapter = BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    assert adapter.allow_all_users is False
    assert adapter._is_early_authorized(EventRef(source="chat", creator_id=999)) is False


def test_multiplex_requires_profile_specific_config_dir(
    tmp_path: Path, monkeypatch
) -> None:
    from basecamp_platform import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "_multiplex_active", lambda: True)
    config = SimpleNamespace(extra={"own_person_id": 50, "allow_from": ["40"]})

    with pytest.raises(ValueError, match="profile-specific"):
        BasecampAdapter(
            config,
            cli=FakeCLI(),
            state_path=tmp_path / "state.json",
            platform=Platform.LOCAL,
        )


def test_multiplex_accepts_isolated_config_dir(tmp_path: Path, monkeypatch) -> None:
    from basecamp_platform import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "_multiplex_active", lambda: True)
    config = SimpleNamespace(
        extra={
            "own_person_id": 50,
            "allow_from": ["40"],
            "config_dir": str(tmp_path / "profile-config"),
        }
    )

    adapter = BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    assert adapter._credential_key.endswith("profile-config")


@pytest.mark.asyncio
async def test_lock_conflict_tuple_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    import gateway.status

    monkeypatch.setattr(
        gateway.status,
        "acquire_scoped_lock",
        lambda *_args, **_kwargs: (False, {"pid": 99999}),
    )
    config = SimpleNamespace(
        extra={"own_person_id": 50, "allow_from": ["40"]}
    )
    adapter = BasecampAdapter(
        config,
        cli=FakeCLI(),
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    assert await adapter.connect() is False
    assert adapter._poll_task is None
    assert adapter._lock_acquired is False


@pytest.mark.asyncio
async def test_connect_releases_lock_on_unexpected_identity_error(
    tmp_path: Path, monkeypatch
) -> None:
    import gateway.status

    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway.status, "acquire_scoped_lock", lambda *_args: True
    )
    monkeypatch.setattr(
        gateway.status,
        "release_scoped_lock",
        lambda scope, key: released.append((scope, key)),
    )

    cli = FakeCLI()

    def fail_identity():
        raise ValueError("malformed profile")

    cli.current_person_id = fail_identity  # type: ignore[method-assign]
    config = SimpleNamespace(extra={"allow_from": ["40"]})
    adapter = BasecampAdapter(
        config,
        cli=cli,
        state_path=tmp_path / "state.json",
        platform=Platform.LOCAL,
    )

    assert await adapter.connect() is False
    assert adapter._lock_acquired is False
    assert released == [("basecamp", adapter._credential_key)]
    assert adapter.fatal_error_code == "identity_failed"
