# Architecture

## Decision

Implement Basecamp as a standalone Hermes platform plugin. Basecamp's official
CLI and Agent Skill remain the command/write layer. Inbound detection uses the
documented personal-notifications endpoint with ETag caching.

## Why a platform adapter

A skill handles agent-initiated Basecamp commands but cannot create inbound
Hermes sessions. The adapter adds event detection, authorization, durable
queueing, session routing, acknowledgement, and response delivery without
modifying Hermes core.

## Data flow

```text
GET /my/readings.json?limit_bubble_ups=true
        │ If-None-Match / 304
        ▼
unread personal notifications
        │ normalize + concrete recording/line identity
        ▼
DurableQueue → early auth → verified Boost
                              │
                              ▼
                     Hermes MessageEvent
                              │
                              ▼
                    persistent session
                              │
                              ▼
official Basecamp CLI → comment / Campfire reply
```

## Trigger scope

The plugin processes unread notifications for the authenticated Basecamp user,
including assignments, subscribed comments, Pings, and project Campfire
activity. Campfire notification-count deltas are resolved to concrete
transcript lines, so every notified line is handled whether or not it mentions
the configured agent. A parallel Mention notification is deduplicated by its
canonical line ID.
Pings are resolved from their Circle and Chat::Transcript to a concrete line,
then boosted and answered through the documented line API. General project
activity and projectless onboarding entries are ignored.

## Identity and context

- Concrete recording and line IDs prevent notification-revision duplicates.
- `chat:<project>:<room>` identifies one Campfire room.
- `ping:<circle>:<transcript>` identifies one Ping conversation.
- `item:<project>:<recording>` identifies one work item.
- Comment notifications map to their parent recording.
- `group_sessions_per_user` defaults to false, so collaborators on the same item
  share one Hermes context.

## Reliability

- The first notification response is baseline only.
- IDs and pending records are persisted atomically with owner-only permissions.
- Authorization happens before notification content is persisted.
- Work remains pending through send and streaming edits and is removed only by
  `on_processing_complete(SUCCESS)`; a gateway restart retries unfinished work.
- Boost creation is idempotent and verified.
- Own notifications are filtered to prevent response loops.
- HTTP 304 reuses the in-memory payload with a zero-byte response body.
- HTTP 429 honors `Retry-After` before one retry.

## Polling

Basecamp exposes no documented agent-facing Web Push registration endpoint.
Version 0.2 polls personal notifications once per second. The measured endpoint
limit is 50 requests per 10 seconds; one request per second uses 20% of that
first limit. ETag/304 caching keeps unchanged response bodies at zero bytes.
