"""Plain-text validation tests for fly.toml and Dockerfile compatibility."""

from pathlib import Path
import re
from typing import List

ROOT = Path(__file__).resolve().parent.parent


def _read_fly_toml() -> str:
    fly_path = ROOT / "fly.toml"
    assert fly_path.is_file(), "fly.toml must exist in the repository root"
    return fly_path.read_text(encoding="utf-8")


def _non_comment_lines(text: str) -> List[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_fly_toml_required_lines() -> None:
    lines = _non_comment_lines(_read_fly_toml())
    expected_lines = [
        "internal_port = 8000",
        "force_https = true",
        "auto_stop_machines = false",
        "min_machines_running = 1",
        "path = '/health'",
        "destination = '/data'",
    ]
    for expected in expected_lines:
        assert expected in lines, f"Missing expected line in fly.toml: {expected}"


def test_fly_toml_single_mount_section() -> None:
    text = _read_fly_toml()
    assert text.count("[[mounts]]") == 1


def test_fly_toml_no_secrets_in_non_comment_lines() -> None:
    secret_pattern = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)\s*=", re.IGNORECASE)
    for line in _non_comment_lines(_read_fly_toml()):
        assert not secret_pattern.search(line), f"Secret assignment found in fly.toml: {line}"
        assert "VIGIL_API_KEY" not in line
        assert "VIGIL_PROJECT_KEYS" not in line


def test_dockerfile_matches_fly_toml_port_and_data_path() -> None:
    dockerfile_path = ROOT / "Dockerfile"
    assert dockerfile_path.is_file()
    lines = _non_comment_lines(dockerfile_path.read_text(encoding="utf-8"))

    cmd_lines = [line for line in lines if line.startswith("CMD ") and not line.startswith("CMD python -c")]
    assert len(cmd_lines) == 1
    cmd_line = cmd_lines[0]
    assert "uvicorn" in cmd_line
    assert '"--host", "0.0.0.0"' in cmd_line or "--host 0.0.0.0" in cmd_line
    assert '"--port", "8000"' in cmd_line or "--port 8000" in cmd_line

    env_data_lines = [
        line for line in lines if line.startswith("ENV ") and "VIGIL_DATA_PATH=" in line
    ]
    assert len(env_data_lines) == 1
    data_path_value = env_data_lines[0].split("VIGIL_DATA_PATH=", 1)[1].strip().strip("\"'")
    assert data_path_value.startswith("/data/")


def test_fly_toml_has_app_and_primary_region() -> None:
    lines = _non_comment_lines(_read_fly_toml())
    assert any(line.startswith("app =") for line in lines)
    assert any(line.startswith("primary_region =") for line in lines)

