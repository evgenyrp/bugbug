import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from app import consumer

FIXTURES = Path(__file__).parent / "fixtures"


def setup_function():
    consumer._seen.clear()


@pytest.fixture(autouse=True)
def _stub_pushlog():
    # Keep the pushlog expansion off the network by default; tests that care
    # about its output re-patch it locally.
    with patch.object(consumer.pushlog, "get_push_commits", return_value=[]):
        yield


def _sample_bodies():
    data = json.loads((FIXTURES / "pulse_messages.json").read_text())
    # The inspector wraps the real AMQP body under "payload".
    return [m["payload"] for m in data]


def _build_msg(task_id="ABC", project="autoland", label="build-linux64/opt"):
    return {
        "status": {"taskId": task_id},
        "runId": 0,
        "task": {
            "tags": {
                "kind": "build",
                "project": project,
                "label": label,
                "createdForUser": "dev@mozilla.com",
            }
        },
    }


def test_sample_messages_are_all_tests_and_skipped():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision") as get_rev,
        patch.object(consumer.client, "trigger_run") as trigger,
    ):
        for body in _sample_bodies():
            assert consumer.process(body, executor) is None
    get_rev.assert_not_called()
    trigger.assert_not_called()
    executor.submit.assert_not_called()


def test_missing_label_is_skipped_not_crashed():
    executor = MagicMock()
    body = {"status": {"taskId": "XYZ"}, "task": {"tags": {"project": "autoland"}}}
    with patch.object(consumer.client, "trigger_run") as trigger:
        assert consumer.process(body, executor) is None
    trigger.assert_not_called()


def test_build_failure_triggers_run_and_submits_poll():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(consumer.client, "trigger_run", return_value="run-1") as trigger,
    ):
        run_id = consumer.process(_build_msg(), executor)

    assert run_id == "run-1"
    trigger.assert_called_once()
    inputs = trigger.call_args.args[0]
    assert inputs["failure_tasks"] == {"build-linux64/opt": "ABC"}
    assert "git_commits" not in inputs
    executor.submit.assert_called_once()
    fn, ctx = executor.submit.call_args.args
    assert fn is consumer.worker.poll_and_notify
    assert ctx.run_id == "run-1"
    assert ctx.git_commit == "deadbeef"
    assert ctx.hg_revision == "hgrev"
    assert ctx.task_id == "ABC"
    assert ctx.repo == "autoland"
    assert ctx.developer_email == "dev@mozilla.com"


def test_failure_tasks_sent_and_commit_authors_recorded():
    executor = MagicMock()
    commits = [
        {"git_commit": "deadbeef", "author_email": "b@m.com"},
        {"git_commit": "aaa", "author_email": "a@m.com"},
    ]
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(consumer.pushlog, "get_push_commits", return_value=commits) as gp,
        patch.object(consumer.client, "trigger_run", return_value="run-1") as trigger,
    ):
        consumer.process(_build_msg(), executor)

    gp.assert_called_once_with("autoland", "hgrev")
    # The agent gets the failing tasks and derives the commits itself.
    assert trigger.call_args.args[0]["failure_tasks"] == {"build-linux64/opt": "ABC"}
    # The listener keeps the commit -> author map to notify the blamed author.
    ctx = executor.submit.call_args.args[1]
    assert ctx.commit_authors == {"deadbeef": "b@m.com", "aaa": "a@m.com"}


def test_same_revision_triggers_once():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(consumer.client, "trigger_run", return_value="run-1") as trigger,
    ):
        consumer.process(_build_msg(task_id="T1"), executor)
        consumer.process(_build_msg(task_id="T2"), executor)

    trigger.assert_called_once()


def test_inherited_failure_is_skipped_before_mapping():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=False),
        patch.object(consumer.lando, "hg_to_git") as hg_to_git,
        patch.object(consumer.client, "trigger_run") as trigger,
    ):
        assert consumer.process(_build_msg(), executor) is None

    hg_to_git.assert_not_called()
    trigger.assert_not_called()
    executor.submit.assert_not_called()


def test_multiple_builds_same_revision_trigger_once():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(consumer.client, "trigger_run", return_value="run-1") as trigger,
    ):
        consumer.process(_build_msg(task_id="T1", label="build-linux64/opt"), executor)
        consumer.process(_build_msg(task_id="T2", label="build-macosx64/opt"), executor)

    trigger.assert_called_once()


def test_inherited_label_does_not_suppress_new_label_on_same_revision():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(
            consumer.regression, "is_new_build_failure", side_effect=[False, True]
        ),
        patch.object(consumer.client, "trigger_run", return_value="run-1") as trigger,
    ):
        # Inherited failure on the first label must not mark the revision seen.
        assert (
            consumer.process(
                _build_msg(task_id="T1", label="build-linux64/opt"), executor
            )
            is None
        )
        # A genuine regression on another label of the same push still runs.
        assert (
            consumer.process(
                _build_msg(task_id="T2", label="build-macosx64/opt"), executor
            )
            == "run-1"
        )

    trigger.assert_called_once()


def test_unwatched_project_skipped_before_api_call():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision") as get_rev,
        patch.object(consumer.client, "trigger_run") as trigger,
    ):
        assert consumer.process(_build_msg(project="try"), executor) is None

    get_rev.assert_not_called()
    trigger.assert_not_called()


def test_unmappable_revision_skipped():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(consumer.lando, "hg_to_git", return_value=None),
        patch.object(consumer.client, "trigger_run") as trigger,
    ):
        assert consumer.process(_build_msg(), executor) is None

    trigger.assert_not_called()
    executor.submit.assert_not_called()


def test_trigger_failure_releases_revision_for_retry():
    executor = MagicMock()
    with (
        patch.object(consumer.taskcluster, "get_hg_revision", return_value="hgrev"),
        patch.object(consumer.lando, "hg_to_git", return_value="deadbeef"),
        patch.object(consumer.regression, "is_new_build_failure", return_value=True),
        patch.object(
            consumer.client, "trigger_run", side_effect=[RuntimeError("boom"), "run-2"]
        ) as trigger,
    ):
        assert consumer.process(_build_msg(task_id="T1"), executor) is None
        # Same revision can be retried because the failed claim was released.
        assert consumer.process(_build_msg(task_id="T2"), executor) == "run-2"

    assert trigger.call_count == 2


def test_queue_name_includes_non_production_environment():
    with patch.object(consumer.settings, "environment", "development"):
        (queue,) = consumer._build_queues("guest")
    assert queue.name == "queue/guest/build-repair-development-task-failed"


def test_queue_name_omits_production_environment():
    with patch.object(consumer.settings, "environment", "production"):
        (queue,) = consumer._build_queues("guest")
    assert queue.name == "queue/guest/build-repair-task-failed"
