from dataclasses import dataclass, field


@dataclass
class RunContext:
    """What the notifier needs about a triggered build-repair run."""

    run_id: str
    repo: str
    git_commit: str
    hg_revision: str
    task_id: str
    developer_email: str | None
    # git commit hash -> author email for the push, to notify the blamed author.
    commit_authors: dict[str, str] = field(default_factory=dict)
