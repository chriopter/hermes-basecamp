# Security Policy

## Trust boundary

Basecamp messages, comments, titles, descriptions, files, and event metadata are untrusted user content. They are delivered as conversational input with `allow_gateway_control=False`; slash-like text cannot invoke Hermes gateway controls.

## Authorization

Configure immutable Basecamp person IDs under `platforms.basecamp.allow_from`. The adapter fails closed when the allowlist is empty. Authorization happens before Boost creation, session lookup, or model dispatch. `allow_all_users: true` is intended only for explicitly trusted accounts.

Boolean authorization settings use strict fail-closed parsing: quoted values
such as `"false"` never enable access. In a multiplexed Hermes gateway,
`extra.config_dir` is mandatory and the adapter takes an exclusive lock on
that resolved Basecamp CLI credential context. Authorization remains in each
profile's `PlatformConfig.extra`; process environment variables cannot expand
another profile's access.

The adapter also filters the authenticated Basecamp identity's own events to prevent response loops.

## Credentials

Authentication is owned by the official Basecamp CLI. This plugin never reads, logs, copies, or exports OAuth tokens. Protect the CLI credential store using the operating-system keyring. If the CLI falls back to `~/.config/basecamp/credentials.json`, require owner-only permissions and treat that file as a bearer credential.

## Data handling

State contains event IDs and pending compact event metadata. Unauthorized
event content is rejected before persistence. The state directory and file
are written below the active Hermes profile home with owner-only permissions
on POSIX systems. Pending work remains durable until Basecamp confirms a
response was accepted. No Basecamp OAuth token is stored in plugin state.

## Reporting

Before public release, report vulnerabilities privately to the repository maintainers. Do not include Basecamp tokens, callback codes, credential files, or customer content in issues.
