# Changelog

## 0.2.0 - Unreleased

- Native Hermes Basecamp platform registration
- Personal unread notifications as the single inbound trigger source
- One-second read-only polling with ETag/304 body caching
- Assignments, subscribed comments, all notified Campfire lines, and Ping routing
- Campfire count-delta line resolution and Mention/Chat deduplication by line ID
- Direct API Boost create/read-back removes three slow CLI calls from acknowledgement
- Legacy unroutable chat aggregates are removed from durable pending state
- Ping Circle/Transcript resolution with line-level Boost and same-thread reply
- Concrete Basecamp recording and line identities prevent notification duplicates
- Durable baseline, deduplication, and pending queue across restarts
- Authorization before persistence, Boost, session creation, or model dispatch
- Immutable person-ID allowlist and self-event filtering
- Immediate idempotent and verified 👀 Boost acknowledgement
- Persistent item and Campfire Hermes contexts
- Delivery-confirmed pending completion and restart retry
- Per-profile Basecamp CLI account/config isolation and credential lock
- Minimal subprocess environment and redacted CLI errors/timeouts
- Optional real Hermes session-ID debug footer via SessionStore
- HTTP 429 `Retry-After` handling
- Unit, security-regression, and Hermes runtime-contract tests
- Room-scoped Campfire line watermarks prevent cross-room history replay
- `chat-lines-v2` performs a one-time no-replay upgrade baseline
- Delivery IDs follow Hermes' merged busy-session turns and complete exactly
  the durable events covered by each confirmed response
- Ambiguous timeouts and permanent CLI/API failures are no longer retried
- Direct HTTP 401 responses refresh through the official CLI once before failing
- Malformed HTTP payloads are normalized without leaking bodies or credential locks
- Disconnect stops the poller before releasing the scoped credential lock
- Lazy wheel entry point avoids circular imports during Hermes discovery
- Direct API sends preserve message IDs and Ping chat metadata
- Ping bursts expand to every unseen transcript line with a no-replay v2 baseline
- Pending authorization is rechecked before every Boost/dispatch
- Corrupt durable state fails closed and is never overwritten as a fresh baseline
- Repeated poll failures transition into Hermes' retryable reconnect flow
- Blocking CLI/HTTP work remains owned until completion during cancellation
- Processing failure/cancellation releases exact in-flight IDs for retry
- External Basecamp timestamps are preserved on native Hermes events
- Delivery correlation is task-local for concurrent per-user sessions
- Queue-mode delivery IDs merge only when Hermes merges the same sender's text
- Edit-in-place streaming for comments, Campfires, and Pings
- Stream status Boost lifecycle: ✏️ while editing, ✅ on success, ❌ on failure
- Durable delivery acknowledgement occurs immediately after confirmed remote send,
  closing the restart replay window before processing completion
- Normal notification deduplication by concrete Basecamp recording ID
- `recording-notifications-v2` no-replay migration removes stale legacy item pending work
- Plain document/file/item changes no longer trigger work without an assignment, comment, or mention
- Direct streaming edits render escaped Markdown as Basecamp rich HTML
- Ping replies normalize outer Gateway `<p>`/`<br>` wrappers without allowing raw HTML
- Input 👀 acknowledgement is removed after every completed, failed, or cancelled turn
- Recommended Basecamp display settings suppress tool/interim bubbles for one visible response
- Assignment notifications are marked as explicit work requests without requiring a mention
- Aggregated comment notifications resolve to concrete comments with no-replay migration
