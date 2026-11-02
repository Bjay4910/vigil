"""Plain-text validation tests for Docker configuration files."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_base_user_and_healthcheck() -> None:
    dockerfile = ROOT / "Dockerfile"
    assert dockerfile.is_file()
    text = dockerfile.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]

    assert lines[0] == "FROM python:3.9-slim"
    user_lines = [line for line in lines if line.startswith("USER ")]
    assert user_lines, "Dockerfile must declare a USER instruction"
    for user_line in user_lines:
        user_val = user_line.split(None, 1)[1].strip()
        assert user_val not in ("root", "0")

    assert any(line.startswith("HEALTHCHECK ") for line in lines)


def test_dockerfile_never_copies_dotenv_or_bakes_api_key() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY .env" not in text

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ENV "):
            assert "API_KEY" not in stripped


def test_dockerignore_excludes_sensitive_and_local_paths() -> None:
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in entries
    assert "data/" in entries
    assert ".git" in entries


def test_docker_compose_key_interpolation_localhost_port_and_volume() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    key_lines = [
        line.strip()
        for line in text.splitlines()
        if not line.strip().startswith("#") and "VIGIL_API_KEY:" in line
    ]
    assert len(key_lines) == 1
    after_colon = key_lines[0].split("VIGIL_API_KEY:", 1)[1].strip()
    assert after_colon.startswith("${")

    assert "127.0.0.1:8000:8000" in text
    assert "vigil-data:/data" in text
    assert "\nvolumes:\n  vigil-data:" in text

