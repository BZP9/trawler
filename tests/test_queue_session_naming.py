"""Regression guard: the queue-runner tmux session must be namespaced per
remote (`trawler-queue-$REMOTE_NAME`), not a bare `trawler-queue` literal.

Two people sharing one GPU box each run their own queue via a differently
named remote (see .env.example / MANUAL.md "Two people, one GPU box"). If any
of these scripts regress to the bare literal, the second person's `enqueue`
would fail to find the first person's already-running session and spawn a
duplicate runner racing over the same queue dir. There's no Python surface
for this (pure bash/tmux), so the scripts' source text is the check.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# a bare `trawler-queue` NOT immediately followed by `-$REMOTE_NAME` (or `-<`
# in doc examples) is the bug this test catches
BARE_LITERAL = re.compile(r"trawler-queue(?!-\$REMOTE_NAME|-<)\b")


def test_offload_sh_namespaces_queue_session_by_remote():
    text = (SCRIPTS / "offload.sh").read_text()
    assert "trawler-queue-$REMOTE_NAME" in text
    assert not BARE_LITERAL.search(text), (
        "offload.sh references a bare 'trawler-queue' tmux session name; "
        "must be 'trawler-queue-$REMOTE_NAME' so two remotes on one box "
        "don't collide"
    )


def test_remote_status_sh_namespaces_queue_session_by_remote():
    text = (SCRIPTS / "remote_status.sh").read_text()
    assert "trawler-queue-$REMOTE_NAME" in text
    assert not BARE_LITERAL.search(text)


def test_remote_queue_sh_comment_documents_namespaced_session():
    text = (SCRIPTS / "remote_queue.sh").read_text()
    assert "trawler-queue-<remote-name>" in text
    assert not BARE_LITERAL.search(text)


def test_env_example_documents_two_person_one_box_setup():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "Two people, SAME" in text
    assert "TRAWLER_REMOTE_STUDIO_1_JOBS" in text


def test_offload_sh_warns_on_remote_name_collision():
    """If two people accidentally pick the same REMOTE_NAME on one box, the
    existing tmux session may belong to someone else's jobs dir — silently
    reusing it would strand the enqueuer's job forever. Both the enqueue path
    and the queue/status display must detect this via a pgrep match on the
    remote_queue.sh invocation's jobs-dir argument, not just has-session."""
    text = (SCRIPTS / "offload.sh").read_text()
    assert text.count('pgrep -f \\"remote_queue.sh $REMOTE_JOBS ') >= 2
    assert "COLLISION" in text


def test_remote_status_sh_warns_on_remote_name_collision():
    text = (SCRIPTS / "remote_status.sh").read_text()
    assert 'pgrep -f \\"remote_queue.sh $REMOTE_JOBS ' in text
    assert "COLLISION" in text
