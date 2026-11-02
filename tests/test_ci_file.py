"""Plain-text validation tests for the GitHub Actions CI workflow."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_file_exists_and_sets_read_only_permissions() -> None:
    assert CI_FILE.is_file()
    text = CI_FILE.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in text


def test_ci_file_specifies_python_39_and_node_22() -> None:
    text = CI_FILE.read_text(encoding="utf-8")
    assert 'python-version: "3.9"' in text
    assert 'node-version: "22"' in text


def test_ci_file_never_references_secrets_or_pull_request_target() -> None:
    text = CI_FILE.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "pull_request_target" not in text


def test_ci_file_runs_pytest_and_npm_test() -> None:
    text = CI_FILE.read_text(encoding="utf-8")
    assert "python -m pytest" in text
    assert "npm test" in text


def test_every_uses_line_is_pinned_to_a_version() -> None:
    text = CI_FILE.read_text(encoding="utf-8")
    uses_lines = [
        line.strip()
        for line in text.splitlines()
        if not line.strip().startswith("#") and "uses:" in line
    ]
    assert uses_lines, "Expected at least one uses: line in ci.yml"
    for line in uses_lines:
        assert "@v" in line, f"Unpinned action in line: {line}"

