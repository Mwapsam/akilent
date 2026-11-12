"""The manual deploy (tools/deploy/remote_release.sh, docs/ops/manual-deploy.md) must release
exactly as the GitHub deploy does. These read both files and fail when they drift apart."""

import re
from pathlib import Path

from django.conf import settings

ROOT = Path(settings.BASE_DIR)
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
REMOTE = (ROOT / "tools/deploy/remote_release.sh").read_text(encoding="utf-8")
LOCAL = (ROOT / "tools/deploy/manual_deploy.sh").read_text(encoding="utf-8")


def _body(source: str, name: str) -> list[str]:
    """The command lines of a shell function `name() { ... }` (comments and blanks dropped)."""
    match = re.search(rf"^\s*{name}\(\) \{{\n(.*?)^\s*\}}", source, re.S | re.M)
    assert match, f"{name}() not found"
    lines = [line.strip() for line in match.group(1).splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_the_manual_release_runs_every_command_the_github_deploy_runs():
    remote = _body(REMOTE, "release")
    for command in _body(WORKFLOW, "release"):
        assert command in remote, (
            f"deploy.yml runs `{command}`; remote_release.sh doesn't"
        )


def test_the_manual_health_check_matches_the_github_one():
    assert _body(REMOTE, "healthy") == _body(WORKFLOW, "healthy")


def test_the_manual_deploy_snapshots_the_database_and_rolls_back():
    assert "pg_dump -Fc --no-owner" in REMOTE
    assert 'git reset --hard "$PREV"' in REMOTE


def test_the_server_script_has_unix_line_endings():
    # Piped to bash on Linux: a Windows line ending breaks every line.
    assert "\r" not in (ROOT / "tools/deploy/remote_release.sh").read_bytes().decode()


def test_the_required_values_match_the_github_check():
    """deploy.yml refuses to deploy without these; the manual deploy checks the same in .env."""
    for name in (
        "DJANGO_SECRET_KEY",
        "ALLOWED_HOSTS",
        "POSTGRES_PASSWORD",
        "FIELD_ENCRYPTION_KEY",
        "INSTAGRAM_VERIFY_TOKEN",
    ):
        assert name in WORKFLOW and name in REMOTE, name


def test_settings_with_secrets_are_never_committed():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tools/deploy/manual_deploy.env" in ignored
    assert "GITHUB_TOKEN=\n" in (
        ROOT / "tools/deploy/manual_deploy.env.example"
    ).read_text(encoding="utf-8")
    assert 'Type "deploy"' in LOCAL  # a typed confirmation before production


def test_the_server_script_is_parsed_whole_before_it_runs():
    """remote_release.sh arrives on bash's stdin; docker compose exec reads stdin too. Unless the
    body is one function called on the last line, the first docker command swallows the rest of
    the script and bash exits early with success (12 Nov 2026: "Done" after only a snapshot)."""
    assert "\nmain() {\n" in REMOTE
    assert REMOTE.rstrip().endswith('main "$@"')
    assert "NOW_LIVE" in LOCAL  # the local half confirms DEPLOY_SHA really changed
