# Hermes Basecamp

A native Basecamp platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It turns personal Basecamp notifications into authorized Hermes sessions and routes responses back to the same item, Campfire, or Ping conversation.

> Technology preview for Hermes Agent 0.20.4+ and Basecamp CLI 0.9.1+.

## Features

- ETag-cached personal-notification polling with one-second latency
- Assignments, concrete comments, mentions, Pings, and notified Campfire lines
- Immutable Basecamp person-ID allowlist and self-event filtering
- 👀 acknowledgement before processing
- Durable pending queue with restart recovery and replay-safe migrations
- Stable sessions per Basecamp item, Campfire room, or Ping transcript
- Edit-in-place streaming for comments, Campfires, and Pings
- ✏️ while streaming, ✅ on success, and ❌ on failure
- Rich Markdown rendering for streamed responses
- No agent turn for plain document, file, message, or item-change notifications

The official [Basecamp Agent Skill](https://github.com/basecamp/skills) remains the command layer. This plugin owns inbound detection, authorization, sessions, acknowledgement, durable delivery, and response transport.

## Install

Install and authenticate Basecamp's official CLI:

```bash
curl -fsSL https://basecamp.com/install-cli | bash
basecamp auth login --remote
basecamp accounts list --json
basecamp doctor --json
```

Install the companion skill and plugin:

```bash
hermes skills install basecamp/skills/basecamp --yes
hermes plugins install chriopter/hermes-basecamp --no-enable
hermes plugins enable basecamp-platform
```

A development checkout under `~/.hermes/plugins/basecamp-platform/` is also supported.

## Configuration

Use `hermes config set`; do not hand-edit `config.yaml`.

```yaml
platforms:
  basecamp:
    enabled: true
    # Immutable Basecamp person IDs. Empty means fail closed.
    allow_from:
      - "123456"
    allow_all_users: false
    extra:
      account: "1234567"
      config_dir: "~/.config"
      poll_interval_seconds: 1
      poll_failure_threshold: 5
      acknowledgement_emoji: "👀"
      stream_progress_emoji: "✏️"
      stream_success_emoji: "✅"
      stream_failure_emoji: "❌"

streaming:
  enabled: true
  transport: auto
  edit_interval: 1.2
  buffer_threshold: 40
  cursor: " ▉"

display:
  platforms:
    basecamp:
      # Keep progress in gateway logs and Basecamp to one answer bubble.
      tool_progress: log
      thinking_progress: false
      interim_assistant_messages: false
```

The adapter discovers its own Basecamp person ID through `/my/profile.json`. In multiplex mode, each profile must use an isolated `extra.config_dir`.

Restart and verify:

```bash
hermes gateway restart
hermes gateway status
```

## Trigger policy

| Basecamp activity | Agent turn |
|---|---|
| Assignment to the authenticated user | Yes |
| Concrete comment notification | Yes |
| Mention notification | Yes |
| Ping line | Yes |
| Notified Campfire line | Yes |
| Plain document/file/message/item change | No |
| Authenticated agent's own activity | No |
| Unauthorized person's activity | No |

Authorization happens before content is persisted, acknowledged, or passed to Hermes. Existing notifications are baselined during first setup and migration.

An `Assignment` notification is an explicit work request for the authenticated
agent and never requires an additional @mention.

## Session model

| Context | Session key |
|---|---|
| Campfire room | `chat:<project-id>:<room-id>` |
| Ping transcript | `ping:<circle-id>:<transcript-id>` |
| Todo, Card, Message, Document, or other item | `item:<project-id>:<recording-id>` |

Comments reuse their parent item's session. Collaborators on the same item share one session by default.

## Streaming behavior

Hermes creates one Basecamp response, stores its recording ID, and progressively replaces that same response.

| Phase | Visible behavior | Reactions | Durable state |
|---|---|---|---|
| Activity detected | Original recording remains unchanged | 👀 on input | Pending |
| First response text | One comment or chat line is created | — | Pending and in-flight |
| Streaming | Same response is edited with accumulated text | ✏️ on response | Pending and in-flight |
| Tool execution | Runs silently; no permanent progress bubble | ✏️ remains | Pending and in-flight |
| Success | Final text replaces the preview | Input 👀 and ✏️ removed; ✅ added | Exact delivery IDs completed |
| Failure | Last written partial text remains | Input 👀 and ✏️ removed; ❌ added | Released for retry |
| Cancellation | No additional response | Input 👀 and ✏️ removed | Released for retry |

Transport routes:

| Context | Create | Edit |
|---|---|---|
| Commentable item | `POST /recordings/{item}/comments.json` | `PUT /comments/{comment}.json` |
| Campfire | `POST /chats/{room}/lines.json` | `PUT /chats/{room}/lines/{line}.json` |
| Ping | `POST /chats/{transcript}/lines.json` | `PUT /chats/{transcript}/lines/{line}.json` |

Status-Boost failures are cosmetic and retried without changing successful text delivery.

## Reliability and security

- Concrete recording and line IDs prevent notification-revision duplicates.
- Pending work is persisted atomically with owner-only permissions.
- Work is acknowledged only after `on_processing_complete(SUCCESS)`.
- Ambiguous POST timeouts are not retried automatically.
- HTTP 429 honors `Retry-After`; OAuth 401 triggers one CLI-managed refresh.
- Corrupt state fails closed instead of being overwritten.
- Basecamp content is untrusted and cannot invoke gateway controls.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), and [RELEASE.md](RELEASE.md).

## Development

```bash
uv venv
uv pip install -e '.[dev]'
PYTHONPATH=/path/to/hermes-agent .venv/bin/python -m pytest -q
uvx ruff check .
PYTHONPATH=/path/to/hermes-agent uvx pyright --pythonpath .venv/bin/python basecamp_platform tests
hermes plugins doctor . --ci
uv build
```

## Roadmap

Basecamp is designing an account-wide resumable event feed with poll and WebSocket lanes. Until that API and its official SDK connector ship, this plugin keeps ETag-cached notification polling and its durable local queue.

## License

MIT. Basecamp is a trademark of 37signals. This is an independent community plugin.