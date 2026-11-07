"""Verify fly.toml never contains alert webhook or digest configuration."""

from pathlib import Path


def test_fly_toml_does_not_contain_alert_settings_or_webhook_hosts() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    fly_toml = (repo_root / "fly.toml").read_text(encoding="utf-8")

    forbidden = (
        "VIGIL_ALERT",
        "VIGIL_DIGEST",
        "discord.com",
        "hooks.slack.com",
    )
    for raw_line in fly_toml.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for token in forbidden:
            assert token not in stripped

