import logging
import re

import httpx

from app import lando
from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)

# Taskcluster ``project`` tag -> hg pushlog repository path. A push and its
# per-commit authors live in the repository it landed on, so we query that
# repo's pushlog rather than the unified mirror. Unlisted projects fall back to
# a same-named repo under the hg base url.
_REPO_PATHS = {
    "autoland": "integration/autoland",
    "mozilla-central": "mozilla-central",
    "mozilla-beta": "releases/mozilla-beta",
    "mozilla-release": "releases/mozilla-release",
    "try": "try",
}

_AUTHOR_RE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")


def _repo_url(repo: str) -> str:
    path = _REPO_PATHS.get(repo, repo)
    return f"{settings.hg_base_url.rstrip('/')}/{path}"


def _parse_author(author: str) -> tuple[str | None, str | None]:
    """Split an hg ``Name <email>`` author string into (name, email)."""
    match = _AUTHOR_RE.match(author or "")
    if not match:
        return (author or None), None
    return match.group("name") or None, match.group("email") or None


def get_push_commits(repo: str, hg_revision: str) -> list[dict]:
    """Return the commits in the push that landed ``hg_revision``, oldest first.

    Each commit is ``{"hg_node", "git_commit", "author_name", "author_email",
    "desc"}``. The pushlog exposes a ``git_changesets`` array parallel to
    ``changesets``; when it is missing a commit's git hash we fall back to the
    per-revision lando mapping. Returns ``[]`` on any error so the caller can
    degrade to treating the tip commit as the sole suspect.
    """
    url = f"{_repo_url(repo)}/json-pushes?changeset={hg_revision}&full=1&version=2"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        pushes = resp.json().get("pushes") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Failed to fetch pushlog for %s@%s: %s", repo, hg_revision, exc)
        return []

    push = next(iter(pushes.values()), None)
    if not push:
        logger.warning("No push found for %s@%s", repo, hg_revision)
        return []

    changesets = push.get("changesets") or []
    git_changesets = push.get("git_changesets") or []

    commits = []
    for i, cs in enumerate(changesets):
        node = cs.get("node") if isinstance(cs, dict) else cs
        if not node:
            continue
        name, email = _parse_author(
            cs.get("author", "") if isinstance(cs, dict) else ""
        )
        git_commit = git_changesets[i] if i < len(git_changesets) else None
        if not git_commit:
            git_commit = lando.hg_to_git(node)
        if not git_commit:
            logger.warning("Could not map push commit %s to git; skipping", node)
            continue
        desc = cs.get("desc", "") if isinstance(cs, dict) else ""
        commits.append(
            {
                "hg_node": node,
                "git_commit": git_commit,
                "author_name": name,
                "author_email": email,
                "desc": (desc or "").splitlines()[0] if desc else "",
            }
        )
    return commits
