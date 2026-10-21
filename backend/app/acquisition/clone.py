"""
Repository acquisition: validate a GitHub URL, clone it with sane limits, and hand
back a local path for the parser layer. This module deliberately knows nothing about
frameworks or parsing -- its only job is "get the code onto disk safely."
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import git
from git.exc import GitCommandError

GITHUB_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(\.git)?/?$"
)

DEFAULT_MAX_REPO_SIZE_MB = 200


class InvalidRepoUrlError(Exception):
    pass


class RepoTooLargeError(Exception):
    pass


class CloneFailedError(Exception):
    pass


@dataclass
class AcquiredRepo:
    local_path: Path
    owner: str
    repo_name: str
    source_url: str
    commit_sha: str

    def cleanup(self) -> None:
        shutil.rmtree(self.local_path, ignore_errors=True)


def validate_github_url(url: str) -> tuple[str, str]:
    """Returns (owner, repo_name) or raises InvalidRepoUrlError."""
    match = GITHUB_URL_PATTERN.match(url.strip())
    if not match:
        raise InvalidRepoUrlError(
            f"'{url}' doesn't look like a GitHub repo URL "
            "(expected https://github.com/<owner>/<repo>)"
        )
    return match.group("owner"), match.group("repo")


def clone_repository(
    url: str,
    max_size_mb: int = DEFAULT_MAX_REPO_SIZE_MB,
    workdir: Path | None = None,
    commit_sha: str | None = None,
) -> AcquiredRepo:
    """
    Shallow-clones the given GitHub repo (depth=1, no full history) into a temp
    directory. Raises InvalidRepoUrlError, CloneFailedError, or RepoTooLargeError.
    Caller is responsible for calling .cleanup() on the returned AcquiredRepo when done.

    `commit_sha` pins the checkout to that exact commit instead of the default
    branch's head -- needed to rebuild a study's inputs after upstream has moved on.
    GitHub serves single commits by SHA, so this stays a depth-1 fetch.
    """
    owner, repo_name = validate_github_url(url)

    dest_root = workdir or Path(tempfile.mkdtemp(prefix="repo-intel-"))
    dest = dest_root / repo_name

    try:
        repo = git.Repo.clone_from(url, dest, depth=1, single_branch=True)
        if commit_sha and repo.head.commit.hexsha != commit_sha:
            repo.git.fetch("--depth", "1", "origin", commit_sha)
            repo.git.checkout(commit_sha)
    except GitCommandError as e:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise CloneFailedError(f"Failed to clone {url}: {e}") from e

    size_mb = _dir_size_mb(dest)
    if size_mb > max_size_mb:
        repo.close()
        shutil.rmtree(dest_root, ignore_errors=True)
        raise RepoTooLargeError(
            f"Repo is {size_mb:.1f}MB, exceeds the {max_size_mb}MB limit. "
            "Increase MAX_REPO_SIZE_MB in .env if this is intentional."
        )

    commit_sha = repo.head.commit.hexsha
    repo.close()

    return AcquiredRepo(
        local_path=dest,
        owner=owner,
        repo_name=repo_name,
        source_url=url,
        commit_sha=commit_sha,
    )


def _dir_size_mb(path: Path) -> float:
    total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total_bytes / (1024 * 1024)
