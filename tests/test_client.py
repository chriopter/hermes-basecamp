from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from basecamp_platform.client import BasecampCLI
from basecamp_platform.core import EventRef


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], dict]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: list[str]) -> dict:
        key = tuple(args)
        self.calls.append(key)
        return self.responses[key]


def test_http_reader_follows_trusted_comment_pagination_links(
    tmp_path, monkeypatch
) -> None:
    from basecamp_platform import client as client_module

    calls: list[str] = []

    class Response:
        def __init__(self, payload, link=None):
            self.payload = payload
            self.headers = Message()
            if link:
                self.headers["Link"] = link

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def open_request(request, timeout):
        calls.append(request.full_url)
        if "page=2" in request.full_url:
            return Response([{"id": 2}])
        return Response(
            [{"id": 1}],
            '<https://3.basecampapi.com/123/comments.json?page=2>; rel="next"',
        )

    monkeypatch.setattr(client_module.urllib.request, "urlopen", open_request)
    reader = client_module.NotificationHTTPReader(
        account="123", config_dir=str(tmp_path)
    )
    monkeypatch.setattr(reader, "_access_token", lambda: "test-token")

    assert reader.request_json_pages("/comments.json") == [{"id": 1}, {"id": 2}]
    assert calls == [
        "https://3.basecampapi.com/123/comments.json",
        "https://3.basecampapi.com/123/comments.json?page=2",
    ]


def test_http_reader_rejects_cross_account_pagination_link(
    tmp_path, monkeypatch
) -> None:
    from basecamp_platform import client as client_module

    class Response:
        headers = Message()
        headers["Link"] = (
            '<https://3.basecampapi.com/999/comments.json?page=2>; rel="next"'
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"[]"

    monkeypatch.setattr(
        client_module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    reader = client_module.NotificationHTTPReader(
        account="123", config_dir=str(tmp_path)
    )
    token_reads = 0

    def token():
        nonlocal token_reads
        token_reads += 1
        return "test-token"

    monkeypatch.setattr(reader, "_access_token", token)

    with pytest.raises(RuntimeError, match="untrusted pagination link"):
        reader.request_json_pages("/comments.json")
    assert token_reads == 1


def test_http_notification_reader_reuses_etag_and_cached_body(
    tmp_path, monkeypatch
) -> None:
    from basecamp_platform import client as client_module

    config_dir = tmp_path / "config"
    credentials = config_dir / "basecamp" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps(
            {
                "https://3.basecampapi.com": {
                    "access_token": "test-token-not-a-secret"
                }
            }
        )
    )
    payload = {"unreads": [{"id": 1}], "reads": []}
    requests = []

    class Response:
        status = 200

        def __init__(self):
            self.headers = Message()
            self.headers["ETag"] = 'W/"abc"'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def open_request(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            return Response()
        headers = Message()
        headers["ETag"] = 'W/"abc"'
        raise urllib.error.HTTPError(
            request.full_url,
            304,
            "Not Modified",
            headers,
            io.BytesIO(b""),
        )

    monkeypatch.setattr(client_module.urllib.request, "urlopen", open_request)
    reader = client_module.NotificationHTTPReader(
        account="123",
        config_dir=str(config_dir),
    )

    assert reader() == payload
    assert reader() == {"unreads": [], "reads": []}
    assert requests[0].get_header("If-none-match") is None
    assert requests[1].get_header("If-none-match") == 'W/"abc"'


def test_notification_reader_normalizes_malformed_json(monkeypatch, tmp_path) -> None:
    from basecamp_platform import client as client_module

    class Response:
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        client_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    reader = client_module.NotificationHTTPReader(
        account="123", config_dir=str(tmp_path)
    )
    monkeypatch.setattr(reader, "_access_token", lambda: "test-token")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        reader()


def test_request_json_accepts_delete_no_content(monkeypatch, tmp_path) -> None:
    from basecamp_platform import client as client_module

    class Response:
        status = 204
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(
        client_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    reader = client_module.NotificationHTTPReader(
        account="123", config_dir=str(tmp_path)
    )
    monkeypatch.setattr(reader, "_access_token", lambda: "test-token")

    assert reader.request_json("/boosts/99.json", method="DELETE") == {}


def test_notification_reader_refreshes_cli_auth_once_on_401(
    monkeypatch, tmp_path
) -> None:
    from basecamp_platform import client as client_module

    refreshed = False
    requests = []

    class Response:
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"unreads": [], "reads": []}'

    def refresh_auth():
        nonlocal refreshed
        refreshed = True

    def open_request(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(b""),
            )
        return Response()

    monkeypatch.setattr(client_module.urllib.request, "urlopen", open_request)
    reader = client_module.NotificationHTTPReader(
        account="123",
        config_dir=str(tmp_path),
        refresh_auth=refresh_auth,
    )
    monkeypatch.setattr(
        reader,
        "_access_token",
        lambda: "new-token" if refreshed else "old-token",
    )

    assert reader() == {"unreads": [], "reads": []}
    assert refreshed is True
    assert requests[0].get_header("Authorization") == "Bearer old-token"
    assert requests[1].get_header("Authorization") == "Bearer new-token"


def test_direct_api_timeout_is_normalized_as_ambiguous(monkeypatch, tmp_path) -> None:
    from basecamp_platform import client as client_module

    def timeout(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(client_module.urllib.request, "urlopen", timeout)
    reader = client_module.NotificationHTTPReader(
        account="123", config_dir=str(tmp_path)
    )
    monkeypatch.setattr(reader, "_access_token", lambda: "test-token")

    with pytest.raises(RuntimeError, match="timed out"):
        reader.request_json("/buckets/1/chats/2/lines.json", method="POST")


def test_collect_events_uses_personal_notifications_as_single_source() -> None:
    comment = {
        "id": 1,
        "type": "Comment",
        "title": "Re: Security review",
        "content_excerpt": "Please check this",
        "created_at": "2026-08-20T10:00:00Z",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
        "creator": {"id": 40, "name": "Chris"},
    }
    assignment = {
        "id": 2,
        "type": "Assignment",
        "title": "Assigned you: Ship plugin",
        "content_excerpt": "Ship plugin",
        "created_at": "2026-08-20T10:00:01Z",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/300",
        "creator": {"id": 40, "name": "Chris"},
    }
    mention = {
        "id": 3,
        "type": "Mention",
        "title": "@mentioned you: Agent please help",
        "content_excerpt": "Agent please help",
        "created_at": "2026-08-20T10:00:02Z",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20@301",
        "creator": {"id": 40, "name": "Chris"},
    }
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {
                    "unreads": [comment, assignment, mention],
                    "reads": [mention],
                    "bubble_ups": [],
                },
            }
        }
    )

    batch = BasecampCLI(runner=runner).collect_events(
        known_buckets={
            "notifications",
            "chat-lines-v2",
            "ping-lines-v2",
            "recording-notifications-v2",
        }
    )

    assert runner.calls == [("notifications", "list", "--limit-bubble-ups")]
    assert batch.buckets == {
        "notifications",
        "chat-lines-v2",
        "ping-lines-v2",
        "recording-notifications-v2",
        "comment-recordings-v1",
    }
    assert len(batch.events) == 3
    by_recording = {event.recording_id: event for event in batch.events}
    assert by_recording[201].source == "notification"
    assert by_recording[201].project_id == 10
    assert by_recording[201].parent_recording_id == 200
    assert by_recording[201].recording_type == "comment"
    assert by_recording[300].recording_type == "todo"
    assert by_recording[301].project_id == 10
    assert by_recording[301].room_id == 20
    assert by_recording[301].recording_type == "chat"


def test_empty_notifications_still_return_known_baseline_bucket() -> None:
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {"unreads": [], "reads": []},
            }
        }
    )

    batch = BasecampCLI(runner=runner).collect_events()

    assert batch.events == []
    assert batch.buckets == {
        "notifications",
        "chat-lines-v2",
        "ping-lines-v2",
        "recording-notifications-v2",
        "comment-recordings-v1",
    }


def test_notification_revision_tracks_concrete_recording_not_unread_counter() -> None:
    versions = [
        {
            "id": 7,
            "type": "Comment",
            "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
            "updated_at": "2026-08-20T10:00:00Z",
            "unread_count": 1,
            "creator": {"id": 40},
        },
        {
            "id": 7,
            "type": "Comment",
            "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
            "updated_at": "2026-08-20T10:01:00Z",
            "unread_count": 2,
            "creator": {"id": 40},
        },
        {
            "id": 7,
            "type": "Comment",
            "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_202",
            "updated_at": "2026-08-20T10:02:00Z",
            "unread_count": 3,
            "creator": {"id": 40},
        },
    ]

    first = BasecampCLI._event_from_notification(versions[0])
    same_recording = BasecampCLI._event_from_notification(versions[1])
    next_recording = BasecampCLI._event_from_notification(versions[2])

    assert first is not None and same_recording is not None and next_recording is not None
    assert first.identity == same_recording.identity
    assert first.event_id == "recording:10:201"
    assert next_recording.event_id == "recording:10:202"


def test_comment_notification_resolves_new_concrete_comments() -> None:
    notification = {
        "id": 7,
        "type": "Comment",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/200/subscription.json",
        "updated_at": "2026-08-20T10:03:00Z",
        "unread_count": 3,
        "creator": {"id": 40},
    }
    comments = [
        {"id": 201, "content": "old", "creator": {"id": 40}},
        {"id": 202, "content": "agent reply", "creator": {"id": 99}},
        {"id": 203, "content": "new request", "creator": {"id": 40}},
    ]
    calls: list[str] = []

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, path, **_kwargs):
            calls.append(path)
            assert path == "/buckets/10/recordings/200/comments.json"
            return comments

    batch = BasecampCLI(notification_reader=Reader()).collect_events(
        seen_identities={"notification:recording:10:201"},
        known_buckets={
            "notifications",
            "chat-lines-v2",
            "ping-lines-v2",
            "recording-notifications-v2",
            "comment-recordings-v1",
        },
        own_person_id=99,
    )

    assert [event.identity for event in batch.events] == [
        "notification:recording:10:203"
    ]
    event = batch.events[0]
    assert event.parent_recording_id == 200
    assert event.content == "new request"
    assert event.creator_id == 40
    assert batch.watermarks == {
        "notification:7:2026-08-20T10:03:00Z:3"
    }

    unchanged = BasecampCLI(notification_reader=Reader()).collect_events(
        seen_identities={
            "notification:recording:10:201",
            *batch.watermarks,
        },
        known_buckets={"comment-recordings-v1"},
        own_person_id=99,
    )
    assert unchanged.events == []
    assert calls == ["/buckets/10/recordings/200/comments.json"]


def test_comment_recording_upgrade_returns_baseline_for_queue_to_skip() -> None:
    notification = {
        "id": 7,
        "type": "Comment",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/200/subscription.json",
        "creator": {"id": 40},
    }
    comments = [
        {"id": 201, "content": "existing", "creator": {"id": 40}}
    ]

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, _path, **_kwargs):
            return comments

    batch = BasecampCLI(notification_reader=Reader()).collect_events(
        known_buckets={
            "notifications",
            "chat-lines-v2",
            "ping-lines-v2",
            "recording-notifications-v2",
        },
        own_person_id=99,
    )

    assert [event.identity for event in batch.events] == [
        "notification:recording:10:201"
    ]
    assert "comment-recordings-v1" in batch.buckets


def test_comment_notification_walks_paginated_comment_thread() -> None:
    notification = {
        "id": 7,
        "type": "Comment",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/200/subscription.json",
        "creator": {"id": 40},
    }
    first_page = [
        {"id": value, "content": f"comment {value}", "creator": {"id": 40}}
        for value in range(201, 216)
    ]
    calls: list[str] = []

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, path, **_kwargs):
            calls.append(path)
            return (
                first_page
                if "?page=" not in path
                else [{"id": 216, "content": "latest", "creator": {"id": 40}}]
            )

    batch = BasecampCLI(notification_reader=Reader()).collect_events(
        known_buckets={"comment-recordings-v1"}, own_person_id=99
    )

    assert calls == [
        "/buckets/10/recordings/200/comments.json",
        "/buckets/10/recordings/200/comments.json?page=2",
    ]
    assert batch.events[-1].recording_id == 216


def test_recording_identity_upgrade_baselines_existing_notifications() -> None:
    notification = {
        "id": 7,
        "type": "Comment",
        "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
        "updated_at": "2026-08-20T10:00:00Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {"unreads": [notification], "reads": []},
            }
        }
    )

    batch = BasecampCLI(runner=runner).collect_events(
        known_buckets={"notifications", "chat-lines-v2", "ping-lines-v2"}
    )

    assert [event.identity for event in batch.events] == [
        "notification:recording:10:201"
    ]
    assert "recording-notifications-v2" in batch.buckets


def test_plain_document_notification_is_not_an_agent_trigger() -> None:
    document = {
        "id": 8,
        "type": "Document",
        "app_url": "https://app.basecamp.com/1/buckets/10/documents/400",
        "updated_at": "2026-08-20T10:00:00Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }
    comment = {
        "id": 9,
        "type": "Comment",
        "app_url": "https://app.basecamp.com/1/buckets/10/documents/400#__recording_401",
        "updated_at": "2026-08-20T10:01:00Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {"unreads": [document, comment], "reads": []},
            }
        }
    )

    events = BasecampCLI(runner=runner).collect_events(
        known_buckets={
            "notifications",
            "chat-lines-v2",
            "ping-lines-v2",
            "recording-notifications-v2",
        }
    ).events

    assert [event.recording_id for event in events] == [401]
    assert events[0].recording_type == "comment"
    assert events[0].parent_recording_id == 400


def test_read_notifications_do_not_trigger_events() -> None:
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {
                    "unreads": [],
                    "reads": [
                        {
                            "id": 8,
                            "type": "Comment",
                            "app_url": "https://app.basecamp.com/1/buckets/10/todos/200#__recording_201",
                            "updated_at": "2026-08-20T10:02:00Z",
                            "creator": {"id": 40},
                        }
                    ],
                },
            }
        }
    )

    assert BasecampCLI(runner=runner).collect_events().events == []


def test_projectless_notifications_are_ignored() -> None:
    runner = FakeRunner(
        {
            ("notifications", "list", "--limit-bubble-ups"): {
                "ok": True,
                "data": {
                    "unreads": [
                        {
                            "id": 1,
                            "type": "Onboarding",
                            "app_url": "https://app.basecamp.com/1/onboarding/messages/2",
                        },
                        {
                            "id": 2,
                            "type": "Chat",
                            "app_url": "https://app.basecamp.com/1/circles/3",
                        },
                    ],
                    "reads": [],
                },
            }
        }
    )

    assert BasecampCLI(runner=runner).collect_events().events == []


def test_chat_line_v2_upgrade_baselines_without_replay() -> None:
    aggregate = {
        "id": 10,
        "type": "Chat",
        "section": "chats",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/20/subscription.json",
        "updated_at": "2026-08-20T10:04:00Z",
        "unread_count": 3,
        "creator": {"id": 40},
    }
    mention = {
        "id": 11,
        "type": "Mention",
        "section": "inbox",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20@301",
        "updated_at": "2026-08-20T10:04:01Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }

    class Reader:
        def __call__(self):
            return {"unreads": [aggregate, mention], "reads": []}

        def request_json(self, *_args, **_kwargs):
            raise AssertionError("migration baseline must not fetch transcripts")

    batch = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    ).collect_events(known_buckets={"notifications"})

    assert batch.events == []
    assert batch.buckets == {
        "notifications",
        "chat-lines-v2",
        "ping-lines-v2",
        "recording-notifications-v2",
        "comment-recordings-v1",
    }


def test_project_chat_notification_resolves_line_and_dedupes_mention() -> None:
    aggregate = {
        "id": 10,
        "type": "Chat",
        "section": "chats",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/20/subscription.json",
        "updated_at": "2026-08-20T10:04:00Z",
        "unread_count": 2,
        "creator": {"id": 40},
        "content_excerpt": "Agent Hi",
    }
    mention = {
        "id": 11,
        "type": "Mention",
        "section": "inbox",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20@301",
        "updated_at": "2026-08-20T10:04:01Z",
        "unread_count": 1,
        "creator": {"id": 40},
        "content_excerpt": "Agent Hi",
    }

    class Reader:
        def __call__(self):
            return {"unreads": [aggregate, mention], "reads": []}

        def request_json(self, path, *, method="GET", payload=None):
            assert method == "GET"
            assert payload is None
            assert path == "/buckets/10/chats/20/lines.json"
            return [
                {
                    "id": 301,
                    "content": "<p>Agent Hi</p>",
                    "created_at": "2026-08-20T10:03:59Z",
                    "creator": {"id": 40, "name": "Chris"},
                }
            ]

    events = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    ).collect_events(
        known_buckets={"notifications", "chat-lines-v2"}
    ).events

    assert len(events) == 1
    assert events[0].event_id == "chat:10:20:line:301"
    assert events[0].recording_id == 301
    assert events[0].room_id == 20
    assert events[0].kind == "notification_chat"
    assert events[0].content == "Agent Hi"


def test_project_chat_detects_new_line_after_unread_count_reset() -> None:
    notification = {
        "id": 10,
        "type": "Chat",
        "section": "chats",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/20",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/20/subscription.json",
        "updated_at": "2026-08-20T10:05:00Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, _path, *, method="GET", payload=None):
            return [
                {
                    "id": 302,
                    "content": "<p>Neue Nachricht</p>",
                    "created_at": "2026-08-20T10:05:00Z",
                    "creator": {"id": 40},
                },
                {
                    "id": 301,
                    "content": "<p>Alte Nachricht</p>",
                    "created_at": "2026-08-20T10:04:00Z",
                    "creator": {"id": 40},
                },
            ]

    events = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    ).collect_events(
        seen_identities={
            "notification:10:2026-08-20T10:04:00Z:5",
            "notification:chat:10:20:line:301",
        },
        known_buckets={"notifications", "chat-lines-v2"},
    ).events

    assert [event.event_id for event in events] == ["chat:10:20:line:302"]


def test_new_project_chat_does_not_replay_transcript_history() -> None:
    notification = {
        "id": 12,
        "type": "Chat",
        "section": "chats",
        "app_url": "https://app.basecamp.com/1/buckets/10/chats/21",
        "subscription_url": "https://3.basecampapi.com/1/buckets/10/recordings/21/subscription.json",
        "updated_at": "2026-08-20T10:06:00Z",
        "unread_count": 1,
        "creator": {"id": 40},
    }

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, _path, *, method="GET", payload=None):
            return [
                {"id": 401, "content": "<p>Aktuell</p>", "creator": {"id": 40}},
                {"id": 400, "content": "<p>Historie</p>", "creator": {"id": 40}},
            ]

    events = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    ).collect_events(
        seen_identities={"notification:chat:10:20:line:301"},
        known_buckets={"notifications", "chat-lines-v2"},
    ).events

    assert [event.event_id for event in events] == ["chat:10:21:line:401"]
    assert events[0].content == "Aktuell"


def test_ping_notification_resolves_latest_line_and_same_reply_context() -> None:
    class Reader:
        def __init__(self):
            self.requests = []
            self.boost_created = False

        def __call__(self):
            return {
                "unreads": [
                    {
                        "id": 9,
                        "type": "Chat",
                        "section": "pings",
                        "app_url": "https://app.basecamp.com/1/circles/50",
                        "subscription_url": "https://3.basecampapi.com/1/buckets/50/recordings/60/subscription.json",
                        "updated_at": "2026-08-20T10:03:00Z",
                        "unread_count": 1,
                        "creator": {"id": 40, "name": "Chris"},
                    }
                ]
            }

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            if method == "GET" and path.endswith("/lines.json"):
                return [
                    {
                        "id": 70,
                        "content": "<p>Agent Test!</p>",
                        "created_at": "2026-08-20T10:03:00Z",
                        "creator": {"id": 40, "name": "Chris"},
                    }
                ]
            if path.endswith("/boosts.json"):
                if method == "POST":
                    self.boost_created = True
                    return {"id": 72}
                return (
                    [{"id": 72, "content": "👀", "booster": {"id": 50}}]
                    if self.boost_created
                    else []
                )
            if method == "POST":
                return {"id": 71}
            return []

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    batch = cli.collect_events(
        known_buckets={"notifications", "chat-lines-v2", "ping-lines-v2"}
    )
    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.project_id == 50
    assert event.room_id == 60
    assert event.recording_id == 70
    assert event.recording_type == "ping"
    assert event.content == "Agent Test!"
    assert cli.ensure_boost(event, own_person_id=50, emoji="👀")["status"] == "confirmed"
    assert cli.reply("ping:50:60", "Hallo") == {"id": 71}
    assert reader.requests[-1] == (
        "POST",
        "/buckets/50/chats/60/lines.json",
        {"content": "<p>Hallo</p>"},
    )


def test_ping_reply_removes_gateway_html_wrappers() -> None:
    class Reader:
        def __init__(self):
            self.requests = []

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            return {"id": 71}

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    cli.reply(
        "ping:50:60",
        "<p>⚠️ Gateway shutting down<br>— Your current task will be interrupted.</p>",
    )

    assert reader.requests == [
        (
            "POST",
            "/buckets/50/chats/60/lines.json",
            {
                "content": (
                    "<p>⚠️ Gateway shutting down<br>"
                    "— Your current task will be interrupted.</p>"
                )
            },
        )
    ]


@pytest.mark.parametrize(
    ("context_id", "message_id", "expected_path"),
    [
        ("ping:50:60", "70", "/buckets/50/chats/60/lines/70.json"),
        ("chat:10:20", "30", "/buckets/10/chats/20/lines/30.json"),
        ("item:10:200", "300", "/buckets/10/comments/300.json"),
    ],
)
def test_edit_reply_routes_context_to_documented_put_endpoint(
    context_id: str, message_id: str, expected_path: str
) -> None:
    class Reader:
        def __init__(self):
            self.requests = []

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            return {"id": int(message_id)}

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    result = cli.edit_reply(context_id, message_id, "Line 1\nLine 2")

    assert result == {"id": int(message_id)}
    assert reader.requests == [
        (
            "PUT",
            expected_path,
            {"content": "<p>Line 1<br>Line 2</p>"},
        )
    ]


def test_edit_reply_renders_markdown_as_safe_basecamp_html() -> None:
    class Reader:
        def __init__(self):
            self.requests = []

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            return {"id": 300}

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    cli.edit_reply(
        "item:10:200",
        "300",
        "**Done** <script>alert(1)</script>",
    )

    rendered = reader.requests[0][2]["content"]
    assert "<strong>Done</strong>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_literal_paragraph_tags_remain_safely_escaped() -> None:
    class Reader:
        def __init__(self):
            self.requests = []

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            return {"id": 300}

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    cli.edit_reply(
        "item:10:200",
        "300",
        "Use <p>literally</p> in documentation.",
    )

    rendered = reader.requests[0][2]["content"]
    assert "&lt;p&gt;literally&lt;/p&gt;" in rendered


def test_stream_status_boost_can_be_created_and_deleted_directly() -> None:
    class Reader:
        def __init__(self):
            self.requests = []

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            return {"id": 99} if method == "POST" else {}

    reader = Reader()
    cli = BasecampCLI(runner=FakeRunner({}), notification_reader=reader)

    boost_id = cli.add_boost(10, "300", "✏️")
    cli.delete_boost(10, boost_id)

    assert boost_id == "99"
    assert reader.requests == [
        (
            "POST",
            "/buckets/10/recordings/300/boosts.json",
            {"content": "✏️"},
        ),
        ("DELETE", "/buckets/10/boosts/99.json", None),
    ]


def test_ping_notification_emits_every_new_line() -> None:
    notification = {
        "id": 9,
        "type": "Chat",
        "section": "pings",
        "app_url": "https://app.basecamp.com/1/circles/50",
        "subscription_url": "https://3.basecampapi.com/1/buckets/50/recordings/60/subscription.json",
        "updated_at": "2026-08-20T10:07:00Z",
        "unread_count": 2,
        "creator": {"id": 40},
    }

    class Reader:
        def __call__(self):
            return {"unreads": [notification], "reads": []}

        def request_json(self, _path, *, method="GET", payload=None):
            return [
                {"id": 72, "content": "<p>second</p>", "creator": {"id": 40}},
                {"id": 71, "content": "<p>first</p>", "creator": {"id": 40}},
            ]

    events = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    ).collect_events(
        known_buckets={"notifications", "chat-lines-v2", "ping-lines-v2"}
    ).events

    assert [event.event_id for event in events] == [
        "ping:50:60:line:71",
        "ping:50:60:line:72",
    ]


def test_boost_is_created_once_and_verified() -> None:
    list_call = ("boost", "list", "300", "--in", "10")
    create_call = ("boost", "create", "300", "👀", "--in", "10")
    responses = {
        list_call: {"ok": True, "data": []},
        create_call: {"ok": True, "data": {"id": 99}},
    }
    runner = FakeRunner(responses)
    original = runner.__call__
    list_count = 0

    def stateful(args: list[str]) -> dict:
        nonlocal list_count
        key = tuple(args)
        if key == list_call:
            list_count += 1
            runner.calls.append(key)
            if list_count == 1:
                return {"ok": True, "data": []}
            return {
                "ok": True,
                "data": [{"id": 99, "content": "👀", "booster": {"id": 50}}],
            }
        return original(args)

    cli = BasecampCLI(runner=stateful)
    event = EventRef(source="notification", project_id=10, recording_id=300)
    result = cli.ensure_boost(event, own_person_id=50, emoji="👀")

    assert result["status"] == "confirmed"
    assert runner.calls == [list_call, create_call, list_call]


def test_project_boost_uses_direct_api_and_readback_when_available() -> None:
    class Reader:
        def __init__(self):
            self.requests = []
            self.created = False

        def __call__(self):
            return {"unreads": []}

        def request_json(self, path, *, method="GET", payload=None):
            self.requests.append((method, path, payload))
            if method == "POST":
                self.created = True
                return {"id": 99}
            return (
                [{"id": 99, "content": "👀", "booster": {"id": 50}}]
                if self.created
                else []
            )

    reader = Reader()
    runner = FakeRunner({})
    cli = BasecampCLI(runner=runner, notification_reader=reader)
    event = EventRef(
        source="notification",
        project_id=10,
        recording_id=300,
        recording_type="chat",
    )

    result = cli.ensure_boost(event, own_person_id=50, emoji="👀")

    assert result == {"status": "confirmed", "target": 300, "boost_id": 99}
    assert runner.calls == []
    assert reader.requests == [
        ("GET", "/buckets/10/recordings/300/boosts.json", None),
        (
            "POST",
            "/buckets/10/recordings/300/boosts.json",
            {"content": "👀"},
        ),
        ("GET", "/buckets/10/recordings/300/boosts.json", None),
    ]


def test_existing_input_boost_returns_id_for_later_removal() -> None:
    class Reader:
        def __call__(self):
            return {"unreads": []}

        def request_json(self, _path, *, method="GET", payload=None):
            assert method == "GET"
            return [{"id": 77, "content": "👀", "booster": {"id": 50}}]

    cli = BasecampCLI(
        runner=FakeRunner({}), notification_reader=Reader()
    )
    event = EventRef(
        source="notification",
        project_id=10,
        recording_id=300,
        recording_type="comment",
    )

    assert cli.ensure_boost(event, own_person_id=50, emoji="👀") == {
        "status": "already_present",
        "target": 300,
        "boost_id": 77,
    }


def test_reply_routes_to_same_chat_or_parent_item() -> None:
    chat_call = ("chat", "post", "Hi", "--in", "10", "--room", "20")
    item_call = ("comments", "create", "200", "Done", "--in", "10")
    runner = FakeRunner(
        {
            chat_call: {"ok": True, "data": {"id": 1}},
            item_call: {"ok": True, "data": {"id": 2}},
        }
    )
    cli = BasecampCLI(runner=runner)

    assert cli.reply("chat:10:20", "Hi")["ok"] is True
    assert cli.reply("item:10:200", "Done")["ok"] is True
    assert runner.calls == [chat_call, item_call]


def test_current_person_id_is_discovered_from_official_api() -> None:
    call = ("api", "get", "/my/profile.json")
    runner = FakeRunner({call: {"ok": True, "data": {"id": 999, "name": "Agent"}}})

    assert BasecampCLI(runner=runner).current_person_id() == 999
    assert runner.calls == [call]


def test_subprocess_runner_redacts_argument_values_on_failure(monkeypatch) -> None:
    from basecamp_platform import client as client_module

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "Bananenbrot secret reply body"

    monkeypatch.setattr(
        client_module.subprocess, "run", lambda *a, **k: FakeCompleted()
    )
    runner = client_module.make_subprocess_runner()
    with pytest.raises(RuntimeError) as exc_info:
        runner(["chat", "post", "Bananenbrot secret reply body", "--in", "10"])

    assert "Bananenbrot" not in str(exc_info.value)
    assert str(exc_info.value) == "basecamp chat post failed (exit 1)"


def test_ok_false_envelope_never_leaks_reply_text() -> None:
    runner = FakeRunner(
        {
            ("chat", "post", "SENSITIVE_REPLY_7f2b", "--in", "10"): {
                "ok": False,
                "error": "failed body=SENSITIVE_REPLY_7f2b",
            }
        }
    )
    cli = BasecampCLI(runner=runner)

    with pytest.raises(RuntimeError) as exc_info:
        cli.run("chat", "post", "SENSITIVE_REPLY_7f2b", "--in", "10")

    assert "SENSITIVE_REPLY_7f2b" not in str(exc_info.value)
    assert str(exc_info.value) == "basecamp chat post failed"


def test_timeout_never_leaks_reply_text(monkeypatch) -> None:
    from basecamp_platform import client as client_module

    secret = "TIMEOUT_SECRET_REPLY_91d4"

    def timeout(command, **kwargs):
        raise client_module.subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(client_module.subprocess, "run", timeout)
    runner = client_module.make_subprocess_runner()

    with pytest.raises(RuntimeError) as exc_info:
        runner(["chat", "post", secret, "--in", "10"])

    assert secret not in str(exc_info.value)
    assert str(exc_info.value) == "basecamp chat post timed out"


def test_process_launch_error_never_leaks_reply_text(monkeypatch) -> None:
    from basecamp_platform import client as client_module

    secret = "LAUNCH_SECRET_REPLY_4ad8"

    def missing(command, **kwargs):
        raise FileNotFoundError(f"could not run {command}")

    monkeypatch.setattr(client_module.subprocess, "run", missing)
    runner = client_module.make_subprocess_runner()

    with pytest.raises(RuntimeError) as exc_info:
        runner(["comments", "create", "10", secret])

    assert secret not in str(exc_info.value)
    assert str(exc_info.value) == "basecamp comments create could not start"
