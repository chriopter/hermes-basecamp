from __future__ import annotations

import os
import tomllib
from pathlib import Path

from basecamp_platform import plugin as plugin_module
from basecamp_platform.plugin import apply_yaml_config, register


class FakeContext:
    def __init__(self):
        self.registration = None

    def register_platform(self, **kwargs):
        self.registration = kwargs


def test_register_exposes_basecamp_platform_and_authorization() -> None:
    context = FakeContext()

    register(context)

    assert context.registration is not None
    registration = context.registration
    assert context.registration["name"] == "basecamp"
    assert "allowed_users_env" not in context.registration
    assert "allow_all_env" not in context.registration
    assert registration["check_fn"]() in {True, False}


def test_yaml_config_keeps_allowlist_profile_scoped(monkeypatch) -> None:
    monkeypatch.delenv("BASECAMP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("BASECAMP_ALLOW_ALL_USERS", raising=False)
    config = {
        "allow_from": ["123"],
        "allow_all_users": False,
        "extra": {
            "poll_interval_seconds": 10,
            "acknowledgement_emoji": "👀",
        },
    }

    extras = apply_yaml_config({}, config)

    assert "BASECAMP_ALLOWED_USERS" not in os.environ
    assert "BASECAMP_ALLOW_ALL_USERS" not in os.environ
    assert extras == {
        "poll_interval_seconds": 10,
        "acknowledgement_emoji": "👀",
        "allow_from": ["123"],
        "group_allow_from": ["123"],
        "allow_all_users": False,
    }


def test_profile_scoped_allow_all_uses_gateway_wildcard() -> None:
    extras = apply_yaml_config({}, {"allow_all_users": True})

    assert extras == {
        "allow_all_users": True,
        "allow_from": ["*"],
        "group_allow_from": ["*"],
    }


def test_quoted_false_never_enables_allow_all() -> None:
    extras = apply_yaml_config({}, {"allow_all_users": "false"})

    assert extras == {"allow_all_users": False}
    assert extras is not None
    assert "allow_from" not in extras
    assert "group_allow_from" not in extras


def test_wheel_entrypoint_uses_lazy_package_module() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )

    assert (
        project["project"]["entry-points"]["hermes_agent.plugins"]
        ["basecamp-platform"]
        == "basecamp_platform"
    )

    import basecamp_platform

    assert callable(basecamp_platform.register)


def test_connected_check_requires_account_and_cli_credentials(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_module, "check_requirements", lambda: True)
    config_dir = tmp_path / "config"
    credentials = config_dir / "basecamp" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}")

    valid = type(
        "Config",
        (),
        {
            "enabled": True,
            "extra": {"account": "123", "config_dir": str(config_dir)},
        },
    )()
    missing_account = type(
        "Config",
        (),
        {"enabled": True, "extra": {"config_dir": str(config_dir)}},
    )()

    assert plugin_module.is_connected(valid) is True
    assert plugin_module.is_connected(missing_account) is False