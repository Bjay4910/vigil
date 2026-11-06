"""Plain-text validation that fly.toml never contains uptime check environment variables."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_fly_toml_never_sets_check_env_vars() -> None:
    fly_path = ROOT / "fly.toml"
    assert fly_path.is_file()
    text = fly_path.read_text(encoding="utf-8")

    non_comment_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in non_comment_lines:
        assert "VIGIL_CHECKS" not in line
        assert "VIGIL_CHECK_INTERVAL" not in line
        assert "VIGIL_CHECK_ENV" not in line

