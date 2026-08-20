"""Hermes plugin registration for the Basecamp platform."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .adapter import BasecampAdapter
from .core import strict_bool


def check_requirements() -> bool:
    return shutil.which("basecamp") is not None


def validate_config(config) -> bool:
    return check_requirements() and bool(getattr(config, "enabled", True))


def is_connected(config) -> bool:
    if not bool(getattr(config, "enabled", False)) or not check_requirements():
        return False
    extra = getattr(config, "extra", {}) or {}
    if not str(extra.get("account") or "").strip():
        return False
    config_root = Path(str(extra.get("config_dir") or "~/.config")).expanduser()
    return (config_root / "basecamp" / "credentials.json").is_file()


def apply_yaml_config(yaml_cfg: dict, basecamp_cfg: dict) -> dict[str, Any] | None:
    raw_extra = basecamp_cfg.get("extra")
    extras: dict[str, Any] = (
        {str(key): value for key, value in raw_extra.items()}
        if isinstance(raw_extra, dict)
        else {}
    )
    for key in (
        "poll_interval_seconds",
        "poll_failure_threshold",
        "acknowledgement_emoji",
        "own_person_id",
    ):
        if key in basecamp_cfg:
            extras.setdefault(key, basecamp_cfg[key])

    allowed = basecamp_cfg.get("allow_from")
    if allowed is not None:
        extras.setdefault("allow_from", allowed)
        extras.setdefault("group_allow_from", allowed)

    allow_all = basecamp_cfg.get("allow_all_users")
    if allow_all is not None:
        enabled = strict_bool(allow_all)
        extras.setdefault("allow_all_users", enabled)
        if enabled:
            extras.setdefault("allow_from", ["*"])
            extras.setdefault("group_allow_from", ["*"])
    return extras or None


def interactive_setup() -> None:
    print("Install and authenticate the official Basecamp CLI:")
    print("  curl -fsSL https://basecamp.com/install-cli | bash")
    print("  basecamp auth login --remote")
    print("  basecamp accounts list --json")
    print("Then enable gateway.platforms.basecamp with `hermes config set`.")


def register(ctx) -> None:
    ctx.register_platform(
        name="basecamp",
        label="Basecamp",
        adapter_factory=lambda cfg: BasecampAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        install_hint="Install the official CLI from https://basecamp.com/agents",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=apply_yaml_config,
        max_message_length=10_000,
        emoji="⛺",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are responding inside Basecamp. Use the official `basecamp` CLI "
            "to inspect related records. Basecamp comments are flat: reply to the "
            "parent item, while Campfire replies stay in the same room."
        ),
    )