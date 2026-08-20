"""Official Basecamp CLI wrapper used by the platform adapter."""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import markdown

from .core import EventBatch, EventRef, parse_context_id, recording_id_from_url

Runner = Callable[[list[str]], dict[str, Any]]
NotificationReader = Callable[[], dict[str, Any]]
CHAT_LINES_STATE_BUCKET = "chat-lines-v2"
PING_LINES_STATE_BUCKET = "ping-lines-v2"
RECORDING_NOTIFICATIONS_STATE_BUCKET = "recording-notifications-v2"
ACTIONABLE_RECORDING_NOTIFICATION_TYPES = {"assignment", "comment", "mention"}


def _redact_command(args: list[str]) -> str:
    """Command shape without argument values (reply text must never leak)."""
    verbs = [part for part in args if part and not part.startswith("-")][:2]
    return "basecamp " + " ".join(verbs) if verbs else "basecamp"


def _compact_text(value: Any, limit: int = 4_000) -> str:
    rendered = html.unescape(str(value or ""))
    rendered = re.sub(r"<[^>]+>", " ", rendered)
    return re.sub(r"\s+", " ", rendered).strip()[:limit]


def _render_basecamp_markdown(text: str) -> str:
    rendered = markdown.markdown(
        html.escape(text),
        extensions=["fenced_code", "nl2br", "sane_lists", "tables"],
        output_format="html",
    )
    return (
        rendered.replace("<br />\n", "<br>")
        .replace("<br />", "<br>")
        .replace("<br>\n", "<br>")
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for candidate in value.values():
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def make_subprocess_runner(
    *,
    account: str | None = None,
    config_dir: str | None = None,
) -> Runner:
    """Build a CLI runner with optional per-profile account/config isolation."""
    import os

    base_env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    if config_dir:
        resolved_config = Path(config_dir).expanduser().resolve()
        resolved_config.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(resolved_config, 0o700)
        base_env["XDG_CONFIG_HOME"] = str(resolved_config)

    def _runner(args: list[str]) -> dict[str, Any]:
        command = ["basecamp", *args, "--json"]
        if account:
            command += ["--account", account]
        try:
            process = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=120,
                env=base_env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{_redact_command(args)} timed out") from None
        except OSError:
            raise RuntimeError(f"{_redact_command(args)} could not start") from None
        if process.returncode != 0:
            raise RuntimeError(
                f"{_redact_command(args)} failed (exit {process.returncode})"
            )
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Basecamp CLI returned invalid JSON") from exc

    return _runner


subprocess_runner = make_subprocess_runner()


class NotificationHTTPReader:
    """Read ``/my/readings.json`` with ETag/304 caching."""

    def __init__(
        self,
        *,
        account: str,
        config_dir: str | None = None,
        refresh_auth: Callable[[], Any] | None = None,
    ):
        self.account = str(account)
        root = (
            Path(config_dir).expanduser().resolve()
            if config_dir
            else (Path.home() / ".config")
        )
        self.credentials_path = root / "basecamp" / "credentials.json"
        self.url = (
            f"https://3.basecampapi.com/{self.account}/my/readings.json"
            "?limit_bubble_ups=true"
        )
        self.etag: str | None = None
        self.cached_payload: dict[str, Any] | None = None
        self.refresh_auth = refresh_auth

    def _access_token(self) -> str:
        try:
            credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            token = str(
                (credentials.get("https://3.basecampapi.com") or {}).get(
                    "access_token"
                )
                or ""
            ).strip()
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("Basecamp CLI credentials are unavailable") from exc
        if not token:
            raise RuntimeError("Basecamp CLI access token is unavailable")
        return token

    def __call__(self) -> dict[str, Any]:
        auth_refreshed = False
        rate_retried = False
        for _attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/json",
                "User-Agent": "Hermes Basecamp Platform Plugin",
            }
            if self.etag:
                headers["If-None-Match"] = self.etag
            request = urllib.request.Request(self.url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    try:
                        payload = json.loads(response.read())
                    except (json.JSONDecodeError, TypeError):
                        raise RuntimeError(
                            "Basecamp notifications returned invalid JSON"
                        ) from None
                    if not isinstance(payload, dict):
                        raise RuntimeError(  # noqa: TRY004 - transport contract
                            "Basecamp notifications returned invalid JSON"
                        )
                    self.etag = response.headers.get("ETag") or self.etag
                    self.cached_payload = payload
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and self.cached_payload is not None:
                    return {"unreads": [], "reads": []}
                if exc.code == 401 and self.refresh_auth and not auth_refreshed:
                    self.refresh_auth()
                    auth_refreshed = True
                    self.etag = None
                    continue
                if exc.code == 429 and not rate_retried:
                    try:
                        retry_after = max(1, min(300, int(exc.headers.get("Retry-After", "1"))))
                    except (TypeError, ValueError):
                        retry_after = 1
                    time.sleep(retry_after)
                    rate_retried = True
                    continue
                raise RuntimeError(
                    f"Basecamp notifications failed (HTTP {exc.code})"
                ) from None
            except TimeoutError:
                raise RuntimeError(
                    "Basecamp notifications request timed out"
                ) from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError) or "timed out" in str(
                    exc.reason
                ).lower():
                    raise RuntimeError(
                        "Basecamp notifications request timed out"
                    ) from None
                raise RuntimeError("Basecamp notifications request failed") from None
            except OSError:
                raise RuntimeError("Basecamp notifications request failed") from None
        raise RuntimeError("Basecamp notifications request failed")

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"https://3.basecampapi.com/{self.account}/{path.lstrip('/')}"
        body = json.dumps(payload).encode() if payload is not None else None
        auth_refreshed = False
        for _attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/json",
                "User-Agent": "Hermes Basecamp Platform Plugin",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        raise RuntimeError(
                            "Basecamp API returned invalid JSON"
                        ) from None
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and self.refresh_auth and not auth_refreshed:
                    self.refresh_auth()
                    auth_refreshed = True
                    continue
                raise RuntimeError(
                    f"Basecamp API request failed (HTTP {exc.code})"
                ) from None
            except TimeoutError:
                raise RuntimeError("Basecamp API request timed out") from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError) or "timed out" in str(
                    exc.reason
                ).lower():
                    raise RuntimeError("Basecamp API request timed out") from None
                raise RuntimeError("Basecamp API request failed") from None
            except OSError:
                raise RuntimeError("Basecamp API request failed") from None
        raise RuntimeError("Basecamp API request failed (HTTP 401)")


class BasecampCLI:
    def __init__(
        self,
        runner: Runner = subprocess_runner,
        notification_reader: NotificationReader | None = None,
    ):
        self.runner = runner
        self.notification_reader = notification_reader

    def run(self, *args: str) -> dict[str, Any]:
        result = self.runner(list(args))
        if not result.get("ok", False):
            # The CLI envelope may echo request bodies. Never propagate it.
            raise RuntimeError(f"{_redact_command(list(args))} failed")
        return result

    def current_person_id(self) -> int:
        data = self.run("api", "get", "/my/profile.json").get("data") or {}
        person_id = data.get("id") if isinstance(data, dict) else None
        if not person_id:
            raise RuntimeError("Basecamp profile did not include a person ID")
        return int(person_id)

    def collect_events(
        self,
        *,
        seen_identities: set[str] | None = None,
        known_buckets: set[str] | None = None,
    ) -> EventBatch:
        """Return the authenticated user's actionable Basecamp notifications."""
        known_buckets = known_buckets or set()
        baseline_chat_lines = CHAT_LINES_STATE_BUCKET not in known_buckets
        baseline_ping_lines = PING_LINES_STATE_BUCKET not in known_buckets
        seen_identities = seen_identities or set()
        data = (
            self.notification_reader()
            if self.notification_reader is not None
            else self.run("notifications", "list", "--limit-bubble-ups").get("data")
        )
        payload = data if isinstance(data, dict) else {}

        notifications: list[dict[str, Any]] = []
        seen_notification_ids: set[str] = set()
        for section in ("unreads",):
            for item in payload.get(section) or []:
                if not isinstance(item, dict):
                    continue
                notification_id = str(item.get("id") or "")
                if not notification_id or notification_id in seen_notification_ids:
                    continue
                seen_notification_ids.add(notification_id)
                notifications.append(item)

        events: list[EventRef] = []
        event_identities: set[str] = set()
        for item in notifications:
            section = str(item.get("section") or "")
            app_url = str(item.get("app_url") or "")
            if baseline_chat_lines and (
                section == "chats" or "/chats/" in app_url
            ):
                continue
            if baseline_ping_lines and section == "pings":
                continue
            if section == "pings":
                candidates = self._events_from_ping_notification(
                    item, seen_identities
                )
            elif section == "chats":
                candidates = self._events_from_chat_notification(item, seen_identities)
            else:
                if str(item.get("type") or "").lower() not in (
                    ACTIONABLE_RECORDING_NOTIFICATION_TYPES
                ):
                    continue
                revision_identity = self._notification_revision_identity(item)
                candidates = [
                    self._event_from_notification(
                        item,
                        revision_already_seen=revision_identity in seen_identities,
                    )
                ]
            for event in candidates:
                if event is None or event.identity in event_identities:
                    continue
                event_identities.add(event.identity)
                events.append(event)
        events.sort(
            key=lambda event: (
                str(event.created_at or ""),
                str(event.event_id or ""),
            )
        )
        return EventBatch(
            events=events,
            buckets={
                "notifications",
                CHAT_LINES_STATE_BUCKET,
                PING_LINES_STATE_BUCKET,
                RECORDING_NOTIFICATIONS_STATE_BUCKET,
            },
        )

    @staticmethod
    def _notification_revision_identity(item: dict[str, Any]) -> str:
        event_id = (
            f"{item.get('id')}:{item.get('updated_at') or item.get('created_at') or ''}:"
            f"{item.get('unread_count') or 0}"
        )
        return f"notification:{event_id}"

    def _events_from_chat_notification(
        self,
        item: dict[str, Any],
        seen_identities: set[str],
    ) -> list[EventRef]:
        subscription_url = str(item.get("subscription_url") or "")
        match = re.search(r"/buckets/(\d+)/recordings/(\d+)/", subscription_url)
        if not match:
            return []
        project_id, chat_id = int(match.group(1)), int(match.group(2))
        if self._notification_revision_identity(item) in seen_identities:
            return []

        notification_id = str(item.get("id") or "")
        previous_count = 0
        prefix = f"notification:{notification_id}:"
        for identity in seen_identities:
            if not identity.startswith(prefix):
                continue
            try:
                previous_count = max(
                    previous_count, int(identity.rsplit(":", 1)[1])
                )
            except (IndexError, ValueError):
                continue

        path = f"/buckets/{project_id}/chats/{chat_id}/lines.json"
        request_json = getattr(self.notification_reader, "request_json", None)
        if callable(request_json):
            lines = _as_list(request_json(path))
        else:
            lines = _as_list(self.run("api", "get", path).get("data"))

        line_prefix = f"notification:chat:{project_id}:{chat_id}:line:"
        has_line_watermark = any(
            identity.startswith(line_prefix) for identity in seen_identities
        )
        selected: list[dict[str, Any]] = []
        if has_line_watermark:
            for line in lines:
                line_id = line.get("id")
                if line_id and f"{line_prefix}{line_id}" in seen_identities:
                    break
                selected.append(line)
        else:
            current_count = int(item.get("unread_count") or 0)
            delta = current_count - previous_count
            new_count = delta if delta > 0 else current_count
            selected = lines[:new_count]

        events: list[EventRef] = []
        for line in reversed(selected):
            line_id = line.get("id")
            if not line_id:
                continue
            creator = line.get("creator") or item.get("creator") or {}
            events.append(
                EventRef(
                    source="notification",
                    event_id=f"chat:{project_id}:{chat_id}:line:{line_id}",
                    project_id=project_id,
                    room_id=chat_id,
                    recording_id=line_id,
                    recording_type="chat",
                    creator_id=creator.get("id"),
                    creator_name=creator.get("name"),
                    content=_compact_text(
                        line.get("content") or item.get("content_excerpt")
                    ),
                    app_url=line.get("app_url") or item.get("app_url"),
                    created_at=line.get("created_at") or item.get("updated_at"),
                    kind="notification_chat",
                )
            )
        return events

    def _events_from_ping_notification(
        self, item: dict[str, Any], seen_identities: set[str]
    ) -> list[EventRef]:
        subscription_url = str(item.get("subscription_url") or "")
        match = re.search(r"/buckets/(\d+)/recordings/(\d+)/", subscription_url)
        if not match:
            return []
        circle_id, chat_id = int(match.group(1)), int(match.group(2))
        if self._notification_revision_identity(item) in seen_identities:
            return []
        path = f"/buckets/{circle_id}/chats/{chat_id}/lines.json"
        request_json = getattr(self.notification_reader, "request_json", None)
        if callable(request_json):
            lines = _as_list(request_json(path))
        else:
            lines = _as_list(self.run("api", "get", path).get("data"))
        if not lines:
            return []

        line_prefix = f"notification:ping:{circle_id}:{chat_id}:line:"
        selected: list[dict[str, Any]] = []
        if any(identity.startswith(line_prefix) for identity in seen_identities):
            for line in lines:
                line_id = line.get("id")
                if line_id and f"{line_prefix}{line_id}" in seen_identities:
                    break
                selected.append(line)
        else:
            selected = lines[: int(item.get("unread_count") or 1)]

        events: list[EventRef] = []
        for line in reversed(selected):
            line_id = line.get("id")
            if not line_id:
                continue
            creator = line.get("creator") or item.get("creator") or {}
            events.append(
                EventRef(
                    source="notification",
                    event_id=f"ping:{circle_id}:{chat_id}:line:{line_id}",
                    project_id=circle_id,
                    room_id=chat_id,
                    recording_id=line_id,
                    recording_type="ping",
                    creator_id=creator.get("id"),
                    creator_name=creator.get("name"),
                    content=_compact_text(
                        line.get("content") or item.get("content_excerpt")
                    ),
                    app_url=line.get("app_url") or item.get("app_url"),
                    created_at=line.get("created_at") or item.get("updated_at"),
                    kind="notification_ping",
                )
            )
        return events

    @staticmethod
    def _event_from_notification(
        item: dict[str, Any], *, revision_already_seen: bool = False
    ) -> EventRef | None:
        app_url = str(item.get("app_url") or "")
        project_match = re.search(r"/buckets/(\d+)/", app_url)
        if not project_match:
            # Onboarding and Ping/circle notifications have no project
            # recording that this adapter can safely reply to.
            return None
        project_id = int(project_match.group(1))
        chat_match = re.search(r"/chats/(\d+)@(\d+)", app_url)
        creator = item.get("creator") or {}
        notification_type = str(item.get("type") or "notification").lower()

        if chat_match:
            room_id = int(chat_match.group(1))
            recording_id = int(chat_match.group(2))
            if revision_already_seen:
                return None
            parent_recording_id = None
            recording_type = "chat"
        else:
            room_id = None
            anchor_match = re.search(r"#__recording_(\d+)", app_url)
            path_id = recording_id_from_url(app_url)
            recording_id = int(anchor_match.group(1)) if anchor_match else path_id
            parent_recording_id = (
                path_id
                if anchor_match and path_id and recording_id != path_id
                else None
            )
            recording_type = (
                "comment"
                if parent_recording_id
                else "todo" if notification_type == "assignment" else notification_type
            )

        if not recording_id:
            return None
        return EventRef(
            source="notification",
            event_id=(
                f"chat:{project_id}:{room_id}:line:{recording_id}"
                if chat_match
                else f"recording:{project_id}:{recording_id}"
            ),
            project_id=project_id,
            room_id=room_id,
            recording_id=recording_id,
            parent_recording_id=parent_recording_id,
            recording_type=recording_type,
            creator_id=creator.get("id"),
            creator_name=creator.get("name"),
            content=_compact_text(
                item.get("content_excerpt") or item.get("title")
            ),
            app_url=app_url,
            created_at=item.get("created_at") or item.get("updated_at"),
            kind=f"notification_{notification_type}",
        )

    def ensure_boost(
        self,
        event: EventRef,
        *,
        own_person_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        if not emoji or not event.project_id or not event.recording_id:
            return {"status": "skipped"}
        target = str(event.recording_id)
        project = str(event.project_id)
        path = f"/buckets/{project}/recordings/{target}/boosts.json"
        request_json = getattr(self.notification_reader, "request_json", None)

        if callable(request_json):
            def direct_boosts() -> list[dict[str, Any]]:
                return _as_list(request_json(path))

            def direct_match(items: list[dict[str, Any]]) -> dict[str, Any] | None:
                return next(
                    (
                        item
                        for item in items
                        if str(item.get("content")) == emoji
                        and (item.get("booster") or item.get("creator") or {}).get("id")
                        == own_person_id
                    ),
                    None,
                )

            existing = direct_match(direct_boosts())
            if existing:
                return {
                    "status": "already_present",
                    "target": event.recording_id,
                    "boost_id": existing.get("id"),
                }
            created = request_json(path, method="POST", payload={"content": emoji})
            return {
                "status": "confirmed"
                if direct_match(direct_boosts())
                else "unverified",
                "target": event.recording_id,
                "boost_id": created.get("id") if isinstance(created, dict) else None,
            }

        if (event.recording_type or "").lower() == "ping":
            def ping_boosts() -> list[dict[str, Any]]:
                return _as_list(self.run("api", "get", path).get("data"))

            def ping_match(items: list[dict[str, Any]]) -> dict[str, Any] | None:
                return next(
                    (
                        item
                        for item in items
                        if str(item.get("content")) == emoji
                        and (item.get("booster") or item.get("creator") or {}).get("id")
                        == own_person_id
                    ),
                    None,
                )

            existing = ping_match(ping_boosts())
            if existing:
                return {
                    "status": "already_present",
                    "target": event.recording_id,
                    "boost_id": existing.get("id"),
                }
            payload = {"content": emoji}
            created = self.run(
                "api", "post", path, "--data", json.dumps(payload)
            ).get("data")
            return {
                "status": "confirmed" if ping_match(ping_boosts()) else "unverified",
                "target": event.recording_id,
                "boost_id": created.get("id") if isinstance(created, dict) else None,
            }

        def boosts() -> list[dict[str, Any]]:
            return _as_list(
                self.run("boost", "list", target, "--in", project).get("data")
            )

        def match(items: list[dict[str, Any]]) -> dict[str, Any] | None:
            for item in items:
                creator = item.get("booster") or item.get("creator") or {}
                if (
                    creator.get("id") == own_person_id
                    and str(item.get("content")) == emoji
                ):
                    return item
            return None

        existing = match(boosts())
        if existing:
            return {
                "status": "already_present",
                "target": event.recording_id,
                "boost_id": existing.get("id"),
            }
        created = self.run("boost", "create", target, emoji, "--in", project)
        return {
            "status": "confirmed" if match(boosts()) else "unverified",
            "target": event.recording_id,
            "boost_id": (created.get("data") or {}).get("id")
            if isinstance(created.get("data"), dict)
            else None,
        }

    def reply(self, context_id: str, text: str) -> dict[str, Any]:
        kind, project_id, resource_id = parse_context_id(context_id)
        if kind == "ping":
            path = f"/buckets/{project_id}/chats/{resource_id}/lines.json"
            payload = {"content": _render_basecamp_markdown(text)}
            request_json = getattr(self.notification_reader, "request_json", None)
            if callable(request_json):
                result = request_json(path, method="POST", payload=payload)
                return result if isinstance(result, dict) else {"data": result}
            return self.run("api", "post", path, "--data", json.dumps(payload))
        if kind == "chat":
            return self.run(
                "chat",
                "post",
                text,
                "--in",
                str(project_id),
                "--room",
                str(resource_id),
            )
        return self.run(
            "comments",
            "create",
            str(resource_id),
            text,
            "--in",
            str(project_id),
        )

    def edit_reply(
        self, context_id: str, message_id: str, text: str
    ) -> dict[str, Any]:
        kind, project_id, resource_id = parse_context_id(context_id)
        if kind in {"chat", "ping"}:
            path = (
                f"/buckets/{project_id}/chats/{resource_id}/"
                f"lines/{message_id}.json"
            )
        else:
            path = f"/buckets/{project_id}/comments/{message_id}.json"
        payload = {"content": _render_basecamp_markdown(text)}
        request_json = getattr(self.notification_reader, "request_json", None)
        if callable(request_json):
            result = request_json(path, method="PUT", payload=payload)
            return result if isinstance(result, dict) else {"data": result}
        return self.run("api", "put", path, "--data", json.dumps(payload))

    def add_boost(self, bucket_id: int, recording_id: str, emoji: str) -> str:
        path = f"/buckets/{bucket_id}/recordings/{recording_id}/boosts.json"
        payload = {"content": emoji}
        request_json = getattr(self.notification_reader, "request_json", None)
        if callable(request_json):
            result = request_json(path, method="POST", payload=payload)
        else:
            result = self.run(
                "boost", "create", recording_id, emoji, "--in", str(bucket_id)
            ).get("data")
        boost_id = result.get("id") if isinstance(result, dict) else None
        if not boost_id:
            raise RuntimeError("Basecamp boost creation returned no id")
        return str(boost_id)

    def delete_boost(self, bucket_id: int, boost_id: str) -> None:
        path = f"/buckets/{bucket_id}/boosts/{boost_id}.json"
        request_json = getattr(self.notification_reader, "request_json", None)
        if callable(request_json):
            request_json(path, method="DELETE")
            return
        self.run("boost", "delete", boost_id, "--in", str(bucket_id))
