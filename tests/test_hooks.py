"""Tests for shared hook helpers."""

import subprocess

from codex_lcm.hooks._common import git_info


def test_git_info_strips_remote_credentials_before_persistence(db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    credential_remote = (
        "https://"
        + "build-user"
        + ":"
        + "synthetic-password"
        + "@example.invalid/org/repo.git?access_token=synthetic-query#fragment"
    )  # pragma: allowlist secret
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", credential_remote],
        check=True,
    )

    remote, branch = git_info(str(repo))
    db.ensure_session("credential-remote", str(repo), remote, branch)

    assert remote == "https://example.invalid/org/repo.git"
    assert db.get_session("credential-remote")["git_remote"] == remote