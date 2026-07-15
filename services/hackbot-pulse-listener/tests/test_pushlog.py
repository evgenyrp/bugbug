from unittest.mock import MagicMock, patch

import httpx
from app import pushlog


def _resp(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def test_repo_url_maps_known_projects():
    assert pushlog._repo_url("autoland") == (
        "https://hg.mozilla.org/integration/autoland"
    )
    assert pushlog._repo_url("mozilla-central") == (
        "https://hg.mozilla.org/mozilla-central"
    )
    # Unknown projects fall back to a same-named repo.
    assert pushlog._repo_url("weird") == "https://hg.mozilla.org/weird"


def test_parse_author_splits_name_and_email():
    assert pushlog._parse_author("Emilio Cobos Álvarez <emilio@crisal.io>") == (
        "Emilio Cobos Álvarez",
        "emilio@crisal.io",
    )
    assert pushlog._parse_author("nobody") == ("nobody", None)
    assert pushlog._parse_author("") == (None, None)


def test_get_push_commits_uses_git_changesets():
    payload = {
        "pushes": {
            "42": {
                "changesets": [
                    {"node": "hg1", "author": "A <a@m.com>", "desc": "first\nbody"},
                    {"node": "hg2", "author": "B <b@m.com>", "desc": "second"},
                ],
                "git_changesets": ["git1", "git2"],
            }
        }
    }
    with patch.object(pushlog.httpx, "get", return_value=_resp(payload)):
        commits = pushlog.get_push_commits("autoland", "hg2")

    assert commits == [
        {
            "hg_node": "hg1",
            "git_commit": "git1",
            "author_name": "A",
            "author_email": "a@m.com",
            "desc": "first",
        },
        {
            "hg_node": "hg2",
            "git_commit": "git2",
            "author_name": "B",
            "author_email": "b@m.com",
            "desc": "second",
        },
    ]


def test_get_push_commits_falls_back_to_lando_when_no_git_changesets():
    payload = {
        "pushes": {"1": {"changesets": [{"node": "hg1", "author": "A <a@m.com>"}]}}
    }
    with (
        patch.object(pushlog.httpx, "get", return_value=_resp(payload)),
        patch.object(pushlog.lando, "hg_to_git", return_value="gitX") as hg2git,
    ):
        commits = pushlog.get_push_commits("autoland", "hg1")

    hg2git.assert_called_once_with("hg1")
    assert commits[0]["git_commit"] == "gitX"


def test_get_push_commits_skips_unmappable_commit():
    payload = {"pushes": {"1": {"changesets": [{"node": "hg1", "author": "A <a@m>"}]}}}
    with (
        patch.object(pushlog.httpx, "get", return_value=_resp(payload)),
        patch.object(pushlog.lando, "hg_to_git", return_value=None),
    ):
        assert pushlog.get_push_commits("autoland", "hg1") == []


def test_get_push_commits_returns_empty_on_http_error():
    with patch.object(pushlog.httpx, "get", side_effect=httpx.HTTPError("boom")):
        assert pushlog.get_push_commits("autoland", "hg1") == []


def test_get_push_commits_returns_empty_when_no_push():
    with patch.object(pushlog.httpx, "get", return_value=_resp({"pushes": {}})):
        assert pushlog.get_push_commits("autoland", "hg1") == []
