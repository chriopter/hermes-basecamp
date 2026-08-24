"""Native Hermes gateway adapter for Basecamp."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import build_session_key

from .client import BasecampCLI, NotificationHTTPReader, make_subprocess_runner
from .core import EventBatch, EventRef, parse_context_id, strict_bool
from .engine import PollingEngine, run_blocking

logger = logging.getLogger(__name__)


def _multiplex_active() -> bool:
    try:
        from agent.secret_scope import is_multiplex_active

        return bool(is_multiplex_active())
    except ImportError:
        return False
    except RuntimeError:
        return True


class BasecampClient(Protocol):
    def current_person_id(self) -> int: ...
    def collect_events(
        self,
        *,
        seen_identities: set[str] | None = None,
        known_buckets: set[str] | None = None,
        own_person_id: int | None = None,
    ) -> EventBatch: ...
    def ensure_boost(
        self, event: EventRef, *, own_person_id: int, emoji: str
    ) -> dict[str, Any]: ...
    def reply(self, context_id: str, text: str) -> dict[str, Any]: ...
    def edit_reply(
        self, context_id: str, message_id: str, text: str
    ) -> dict[str, Any]: ...
    def add_boost(self, bucket_id: int, recording_id: str, emoji: str) -> str: ...
    def delete_boost(self, bucket_id: int, boost_id: str) -> None: ...


class BasecampAdapter(BasePlatformAdapter):
    def __init__(
        self,
        config,
        *,
        cli: BasecampClient | None = None,
        state_path: Path | None = None,
        platform: Platform | None = None,
    ):
        resolved_platform = platform or Platform("basecamp")
        super().__init__(config=config, platform=resolved_platform)
        extra = getattr(config, "extra", {}) or {}
        extra.setdefault("group_sessions_per_user", False)
        account = extra.get("account")
        config_dir = extra.get("config_dir")
        if _multiplex_active() and not config_dir:
            raise ValueError(
                "Basecamp requires a profile-specific extra.config_dir in a multiplex gateway"
            )
        self._credential_key = str(
            Path(str(config_dir)).expanduser().resolve()
            if config_dir
            else Path("~/.config").expanduser().resolve()
        )
        self._lock_acquired = False
        if cli is not None:
            self.cli = cli
        else:
            runner = make_subprocess_runner(
                account=str(account) if account else None,
                config_dir=str(config_dir) if config_dir else None,
            )
            refresh_cli = BasecampCLI(runner=runner)
            notification_reader = (
                NotificationHTTPReader(
                    account=str(account),
                    config_dir=str(config_dir) if config_dir else None,
                    refresh_auth=refresh_cli.current_person_id,
                )
                if account
                else None
            )
            self.cli = BasecampCLI(
                runner=runner,
                notification_reader=notification_reader,
            )
        self.poll_interval = max(1, min(300, int(extra.get("poll_interval_seconds", 30))))
        self.poll_failure_threshold = max(
            1, min(100, int(extra.get("poll_failure_threshold", 5)))
        )
        self.emoji = str(extra.get("acknowledgement_emoji", "👀"))[:16]
        self.own_person_id = int(extra.get("own_person_id", 0) or 0)
        configured_allowed = extra.get("group_allow_from") or extra.get("allow_from") or []
        if isinstance(configured_allowed, str):
            configured_allowed = configured_allowed.split(",")
        self.allowed_users = {
            str(value).strip() for value in configured_allowed if str(value).strip()
        }
        self.allow_all_users = strict_bool(extra.get("allow_all_users", False))
        self.debug_session_footer = strict_bool(extra.get("debug_session_footer", False))
        self._context_session_keys: dict[str, str] = {}
        self._delivery_context: ContextVar[
            tuple[str, list[str]] | None
        ] = ContextVar("basecamp_delivery_context", default=None)
        self._completion_retry_ids: set[str] = set()
        self._confirmed_delivery_ids: set[str] = set()
        self._stream_statuses: dict[tuple[str, ...], tuple[int, str, str]] = {}
        self._stream_status_retries: dict[
            tuple[str, ...], tuple[int, str, str]
        ] = {}
        self._ack_removal_retries: set[tuple[int, str]] = set()
        self.stream_progress_emoji = str(extra.get("stream_progress_emoji", "✏️"))[:16]
        self.stream_success_emoji = str(extra.get("stream_success_emoji", "✅"))[:16]
        self.stream_failure_emoji = str(extra.get("stream_failure_emoji", "❌"))[:16]
        if state_path is None:
            from hermes_constants import get_hermes_home

            state_path = get_hermes_home() / "state" / "basecamp-platform.json"
        self.state_path = Path(state_path)
        self.engine = PollingEngine(
            client=self.cli,
            state_path=self.state_path,
            own_person_id=self.own_person_id,
            emoji=self.emoji,
            dispatch=self._dispatch,
            authorize=self._is_early_authorized,
        )
        self._poll_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return "Basecamp"

    @staticmethod
    def _send_error_retryable(error: str) -> bool:
        lowered = error.lower()
        if "timed out" in lowered:
            return False
        exit_match = re.search(r"exit\s+(\d+)", lowered)
        if exit_match:
            return int(exit_match.group(1)) == 5
        http_match = re.search(r"http\s+(\d+)", lowered)
        if http_match:
            status = int(http_match.group(1))
            return status == 429
        return False

    @staticmethod
    def _event_timestamp(value: str | None) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    def _is_early_authorized(self, event: EventRef) -> bool:
        if self.allow_all_users:
            return True
        return bool(event.creator_id) and str(event.creator_id) in self.allowed_users

    def _release_credential_lock(self) -> None:
        if not self._lock_acquired:
            return
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("basecamp", self._credential_key)
        except ImportError:
            pass
        self._lock_acquired = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            from gateway.status import acquire_scoped_lock

            lock_result = acquire_scoped_lock("basecamp", self._credential_key)
            acquired = (
                bool(lock_result[0])
                if isinstance(lock_result, tuple)
                else bool(lock_result)
            )
            if not acquired:
                self._set_fatal_error(
                    "lock_conflict",
                    "Basecamp credential context is already used by another profile",
                    retryable=False,
                )
                return False
            self._lock_acquired = True
        except ImportError:
            pass
        if not self.own_person_id:
            try:
                self.own_person_id = await run_blocking(self.cli.current_person_id)
                self.engine.own_person_id = self.own_person_id
                self.engine.queue.own_person_id = self.own_person_id
            except asyncio.CancelledError:
                self._release_credential_lock()
                raise
            except Exception as exc:  # noqa: BLE001 - lock cleanup boundary
                message = (
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else "Basecamp identity discovery failed"
                )
                self._set_fatal_error("identity_failed", message, retryable=True)
                self._release_credential_lock()
                return False
        try:
            await self.engine.poll_once()
        except asyncio.CancelledError:
            self._release_credential_lock()
            raise
        except Exception as exc:  # noqa: BLE001 - lock cleanup boundary
            message = (
                str(exc)
                if isinstance(exc, RuntimeError)
                else "Basecamp initial poll failed"
            )
            logger.error("Basecamp initial poll failed: %s", message)
            self._set_fatal_error("connect_failed", message, retryable=True)
            self._release_credential_lock()
            return False
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        self._release_credential_lock()
        self._mark_disconnected()

    async def _poll_loop(self) -> None:
        consecutive_failures = 0
        while True:
            started = time.monotonic()
            try:
                self._flush_completion_retries()
                await self._flush_stream_status_retries()
                await self._flush_ack_removal_retries()
                await self.engine.poll_once()
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                logger.exception("Basecamp poll failed; retrying")
                if consecutive_failures >= self.poll_failure_threshold:
                    self._set_fatal_error(
                        "poll_failed",
                        "Basecamp polling failed repeatedly",
                        retryable=True,
                    )
                    await self._notify_fatal_error()
                    return
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.poll_interval - elapsed))

    async def _dispatch(
        self,
        event: EventRef,
        context_id: str,
        boost_result: dict[str, Any],
    ) -> None:
        event_identity = event.identity
        source = self.build_source(
            chat_id=context_id,
            chat_name=f"Basecamp {context_id}",
            chat_type="group",
            user_id=str(event.creator_id) if event.creator_id else None,
            user_name=event.creator_name,
            scope_id=str(event.project_id) if event.project_id else None,
            message_id=event_identity,
        )
        self._context_session_keys[context_id] = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get(
                "group_sessions_per_user", True
            ),
            thread_sessions_per_user=self.config.extra.get(
                "thread_sessions_per_user", False
            ),
            profile=self._session_key_profile(source),
        )
        payload = {
            "source": event.source,
            "kind": event.kind,
            "project_id": event.project_id,
            "recording_id": event.recording_id,
            "parent_recording_id": event.parent_recording_id,
            "app_url": event.app_url,
            "acknowledgement": boost_result,
            "acknowledgements": [boost_result]
            if boost_result.get("boost_id")
            else [],
            "delivery_ids": [event_identity],
        }
        text = event.content or "Basecamp activity detected"
        if event.kind == "notification_assignment":
            text = (
                "Basecamp assignment: this item is assigned to the agent and is an "
                "explicit work request. No @mention is required.\n\n"
                + text
            )
        text += "\n\nBasecamp event metadata:\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        )
        message = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=str(event.creator_id) if event.creator_id else None,
            user_name=event.creator_name,
            source=source,
            raw_message=event.to_dict(),
            message_id=event_identity,
            timestamp=self._event_timestamp(event.created_at),
            metadata={"basecamp": payload},
            allow_gateway_control=False,
        )
        await self.handle_message(message)

    @staticmethod
    def _merge_delivery_ids(target: MessageEvent, incoming: MessageEvent) -> None:
        target_basecamp = (target.metadata or {}).setdefault("basecamp", {})
        incoming_ids = ((incoming.metadata or {}).get("basecamp") or {}).get(
            "delivery_ids", []
        )
        target_ids = target_basecamp.setdefault("delivery_ids", [])
        for identity in incoming_ids:
            if identity not in target_ids:
                target_ids.append(identity)
        target_acks = target_basecamp.setdefault("acknowledgements", [])
        for basecamp in (
            target_basecamp,
            (incoming.metadata or {}).get("basecamp") or {},
        ):
            candidates = list(basecamp.get("acknowledgements") or [])
            single = basecamp.get("acknowledgement")
            if single:
                candidates.append(single)
            for acknowledgement in candidates:
                boost_id = acknowledgement.get("boost_id")
                if boost_id and not any(
                    item.get("boost_id") == boost_id for item in target_acks
                ):
                    target_acks.append(acknowledgement)

    async def handle_message(self, event: MessageEvent) -> None:
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get(
                "group_sessions_per_user", True
            ),
            thread_sessions_per_user=self.config.extra.get(
                "thread_sessions_per_user", False
            ),
            profile=self._session_key_profile(event.source),
        )
        if session_key in self._active_sessions:
            state = self._text_debounce_store().get(session_key)
            if state is not None:
                if self._can_merge_text_debounce_events(state.event, event):
                    self._merge_delivery_ids(state.event, event)
            else:
                pending = self._pending_messages.get(session_key)
                if pending is not None and (
                    getattr(self, "_busy_text_mode", "interrupt") != "queue"
                    or self._can_merge_text_debounce_events(pending, event)
                ):
                    self._merge_delivery_ids(pending, event)
        await super().handle_message(event)

    async def on_processing_start(self, event: MessageEvent) -> None:
        delivery_ids = ((event.metadata or {}).get("basecamp") or {}).get(
            "delivery_ids", []
        )
        if delivery_ids:
            self._delivery_context.set(
                (event.source.chat_id, list(delivery_ids))
            )

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        await self._remove_input_acknowledgement(event)
        delivery_ids = list(
            ((event.metadata or {}).get("basecamp") or {}).get(
                "delivery_ids", []
            )
        )
        stream_key = tuple(delivery_ids)
        stream_status = self._stream_statuses.get(stream_key)
        if stream_status is not None:
            final_emoji = (
                self.stream_success_emoji
                if outcome == ProcessingOutcome.SUCCESS
                else self.stream_failure_emoji
                if outcome == ProcessingOutcome.FAILURE
                else ""
            )
            await self._set_stream_status(
                stream_key,
                stream_status[0],
                stream_status[1],
                final_emoji,
            )
        self._delivery_context.set(None)
        for identity in delivery_ids:
            if outcome == ProcessingOutcome.SUCCESS:
                self._complete_confirmed_delivery(identity)
            else:
                self.engine.retry(identity)
        self._confirmed_delivery_ids.difference_update(delivery_ids)

    def _complete_confirmed_delivery(self, identity: str) -> None:
        if identity in self._confirmed_delivery_ids:
            return
        try:
            self.engine.complete(identity)
        except Exception:  # noqa: BLE001 - retry local ACK later
            self._completion_retry_ids.add(identity)
        self._confirmed_delivery_ids.add(identity)

    def _flush_completion_retries(self) -> None:
        for identity in tuple(self._completion_retry_ids):
            try:
                self.engine.complete(identity)
            except Exception:  # noqa: BLE001 - keep ACK retry in-process
                logger.warning("Basecamp local pending completion still failing")
                continue
            self._completion_retry_ids.discard(identity)

    def _stream_key(self, chat_id: str, message_id: str) -> tuple[str, ...]:
        delivery_context = self._delivery_context.get()
        if delivery_context and delivery_context[0] == chat_id:
            return tuple(delivery_context[1])
        return (f"response:{chat_id}:{message_id}",)

    async def _set_stream_status(
        self,
        key: tuple[str, ...],
        bucket_id: int,
        message_id: str,
        emoji: str,
    ) -> None:
        current = self._stream_statuses.get(key)
        if current is not None:
            try:
                await run_blocking(self.cli.delete_boost, current[0], current[2])
            except Exception:  # noqa: BLE001 - cosmetic status boundary
                logger.warning("Basecamp stream status Boost removal failed")
                self._stream_status_retries[key] = (
                    bucket_id,
                    message_id,
                    emoji,
                )
                return
            self._stream_statuses.pop(key, None)
        if not emoji:
            self._stream_status_retries.pop(key, None)
            return
        try:
            boost_id = await run_blocking(
                self.cli.add_boost, bucket_id, message_id, emoji
            )
        except Exception:  # noqa: BLE001 - cosmetic status boundary
            logger.warning("Basecamp stream status Boost creation failed")
            self._stream_status_retries[key] = (
                bucket_id,
                message_id,
                emoji,
            )
            return
        self._stream_status_retries.pop(key, None)
        if emoji == self.stream_progress_emoji:
            self._stream_statuses[key] = (bucket_id, message_id, boost_id)

    async def _flush_stream_status_retries(self) -> None:
        for key, (bucket_id, message_id, emoji) in tuple(
            self._stream_status_retries.items()
        ):
            await self._set_stream_status(
                key, bucket_id, message_id, emoji
            )

    async def _remove_input_acknowledgement(self, event: MessageEvent) -> None:
        basecamp = (event.metadata or {}).get("basecamp") or {}
        bucket_id = basecamp.get("project_id")
        if not bucket_id:
            return
        acknowledgements = list(basecamp.get("acknowledgements") or [])
        single = basecamp.get("acknowledgement")
        if single:
            acknowledgements.append(single)
        boost_ids: list[str] = []
        for item in acknowledgements:
            boost_id = item.get("boost_id")
            if boost_id and str(boost_id) not in boost_ids:
                boost_ids.append(str(boost_id))
        for boost_id in boost_ids:
            target = (int(bucket_id), boost_id)
            try:
                await run_blocking(self.cli.delete_boost, target[0], target[1])
            except Exception:  # noqa: BLE001 - cosmetic acknowledgement boundary
                logger.warning("Basecamp input acknowledgement removal failed")
                self._ack_removal_retries.add(target)
                continue
            self._ack_removal_retries.discard(target)

    async def _flush_ack_removal_retries(self) -> None:
        for bucket_id, boost_id in tuple(self._ack_removal_retries):
            try:
                await run_blocking(self.cli.delete_boost, bucket_id, boost_id)
            except Exception as exc:  # noqa: BLE001 - retry cosmetic acknowledgement
                logger.debug(
                    "Basecamp input acknowledgement removal retry failed: %s",
                    exc,
                )
                continue
            self._ack_removal_retries.discard((bucket_id, boost_id))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        try:
            result = await run_blocking(
                self.cli.edit_reply, chat_id, message_id, content
            )
        except RuntimeError as exc:
            error = str(exc)
            return SendResult(
                success=False,
                error=error,
                retryable=self._send_error_retryable(error),
            )
        except Exception:
            logger.exception("Unexpected Basecamp edit failure")
            return SendResult(
                success=False,
                error="Basecamp edit failed",
                retryable=False,
            )

        key = self._stream_key(chat_id, message_id)
        bucket_id = parse_context_id(chat_id)[1]
        if not finalize and key not in self._stream_statuses:
            await self._set_stream_status(
                key, bucket_id, message_id, self.stream_progress_emoji
            )
        return SendResult(
            success=True,
            message_id=message_id,
            raw_response=result,
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            if self.debug_session_footer and "Debug: Hermes-Session" not in content:
                session_key = self._context_session_keys.get(chat_id)
                store = getattr(self, "_session_store", None)
                peek = getattr(store, "peek_session_id", None) if store else None
                if session_key and callable(peek):
                    session_id = str(peek(session_key) or "").strip()
                    if session_id:
                        content += f"\n\nDebug: Hermes-Session {session_id}"
            result = await run_blocking(self.cli.reply, chat_id, content)
        except RuntimeError as exc:
            error = str(exc)
            return SendResult(
                success=False,
                error=error,
                retryable=self._send_error_retryable(error),
            )
        except Exception:
            logger.exception("Unexpected Basecamp send failure")
            return SendResult(
                success=False,
                error="Basecamp send failed",
                retryable=False,
            )

        data = result.get("data") or result
        message_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
        delivery_context = self._delivery_context.get()
        if delivery_context and delivery_context[0] == chat_id:
            for identity in delivery_context[1]:
                self._complete_confirmed_delivery(identity)
        return SendResult(
            success=True,
            message_id=message_id,
            raw_response=result,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        kind, _project_id, resource_id = parse_context_id(chat_id)
        return {
            "chat_id": chat_id,
            "name": f"Basecamp {kind} {resource_id}",
            "type": "group",
        }
