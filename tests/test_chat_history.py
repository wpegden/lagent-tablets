from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lagent_tablets.chat_history import (
    commit_chat_checkpoint,
    ensure_chat_repo,
    rewind_chat_history,
)
from lagent_tablets.git_ops import commit_cycle, init_repo


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestChatHistoryGit(unittest.TestCase):
    def test_commit_cycle_tags_nested_chat_repo(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / ".agent-supervisor").mkdir()
        init_repo(repo)
        (repo / ".agent-supervisor" / "viewer_state.json").write_text(
            json.dumps({"state": {"cycle": 1}, "tablet": {"nodes": {}}, "nodes": {}, "meta": {"source": "cycle"}}),
            encoding="utf-8",
        )
        chats = ensure_chat_repo(repo)
        artifact = chats / "cycle-0001" / "worker_handoff"
        artifact.mkdir(parents=True)
        (artifact / "prompt.txt").write_text("worker prompt", encoding="utf-8")
        (artifact / "output.log").write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}) + "\n",
            encoding="utf-8",
        )

        commit_cycle(repo, 1, phase="theorem_stating", outcome="PROGRESS")

        tags = _git(chats, "tag", "-l", "cycle-*").splitlines()
        self.assertIn("cycle-1", tags)
        prompt = _git(chats, "show", "cycle-1:cycle-0001/worker_handoff/prompt.txt")
        self.assertEqual(prompt, "worker prompt")

    def test_rewind_chat_history_resets_nested_repo(self) -> None:
        repo = Path(tempfile.mkdtemp())
        chats = ensure_chat_repo(repo)

        first = chats / "cycle-0001" / "worker_handoff"
        first.mkdir(parents=True)
        (first / "prompt.txt").write_text("cycle1", encoding="utf-8")
        commit_chat_checkpoint(repo, tag="cycle-1")

        second = chats / "cycle-0002" / "worker_handoff"
        second.mkdir(parents=True)
        (second / "prompt.txt").write_text("cycle2", encoding="utf-8")
        commit_chat_checkpoint(repo, tag="cycle-2")

        rewind_chat_history(repo, tag="cycle-1")

        self.assertTrue((chats / "cycle-0001" / "worker_handoff" / "prompt.txt").exists())
        self.assertFalse((chats / "cycle-0002").exists())
        tags = _git(chats, "tag", "-l", "cycle-*").splitlines()
        self.assertIn("cycle-1", tags)
        self.assertNotIn("cycle-2", tags)


@unittest.skipUnless(shutil.which("node"), "node is required for viewer server tests")
class TestViewerChatReaders(unittest.TestCase):
    def test_server_reads_live_and_historical_chats_from_nested_repo(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / ".agent-supervisor" / "viewer").mkdir(parents=True)
        (repo / "lagent.config.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
        (repo / ".agent-supervisor" / "viewer_state.json").write_text(
            json.dumps({"state": {"cycle": 1}, "tablet": {"nodes": {}}, "nodes": {}, "meta": {"source": "worker", "in_flight_cycle": 1}}),
            encoding="utf-8",
        )
        (repo / ".agent-supervisor" / "viewer" / "viewer-state.json").write_text(
            json.dumps({"state": {"cycle": 1}, "tablet": {"nodes": {}}, "nodes": {}, "meta": {"source": "worker", "in_flight_cycle": 1}}),
            encoding="utf-8",
        )

        chats = ensure_chat_repo(repo)
        artifact = chats / "cycle-0001" / "worker_handoff"
        artifact.mkdir(parents=True)
        (artifact / "prompt.txt").write_text("prompt text", encoding="utf-8")
        (artifact / "output.log").write_text(
            "\n".join([
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first reply"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second reply"}}),
            ]) + "\n",
            encoding="utf-8",
        )
        commit_chat_checkpoint(repo, tag="cycle-1")

        script = """
const { readLiveChats, readHistoricalChats } = require('./viewer/server.js');
const repo = process.argv[1];
console.log(JSON.stringify({
  live: readLiveChats(repo),
  historical: readHistoricalChats(repo, 1),
}));
"""
        result = subprocess.run(
            ["node", "-e", script, str(repo)],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["live"]["cycle"], 1)
        self.assertEqual(data["historical"]["cycle"], 1)
        self.assertEqual(data["historical"]["artifacts"][0]["title"], "Worker")
        self.assertEqual(len(data["historical"]["artifacts"][0]["entries"]), 3)  # prompt + 2 replies

    def test_server_orders_chat_artifacts_by_stage(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / ".agent-supervisor").mkdir(parents=True)
        (repo / "lagent.config.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
        (repo / ".agent-supervisor" / "viewer_state.json").write_text(
            json.dumps({"state": {"cycle": 1}, "tablet": {"nodes": {}}, "nodes": {}, "meta": {"source": "worker", "in_flight_cycle": 1}}),
            encoding="utf-8",
        )

        chats = ensure_chat_repo(repo)
        base = chats / "cycle-0001"
        for artifact_name in [
            "reviewer_decision_attempt_0001",
            "nl_proof_target_node_2_attempt_0001",
            "correspondence_result_1_attempt_0001",
            "worker_handoff_attempt_0001",
            "correspondence_result_0_attempt_0001",
        ]:
            artifact = base / artifact_name
            artifact.mkdir(parents=True)
            (artifact / "prompt.txt").write_text(artifact_name, encoding="utf-8")

        commit_chat_checkpoint(repo, tag="cycle-1")

        script = """
const { readHistoricalChats } = require('./viewer/server.js');
const repo = process.argv[1];
console.log(JSON.stringify(readHistoricalChats(repo, 1).artifacts.map(a => a.id)));
"""
        result = subprocess.run(
            ["node", "-e", script, str(repo)],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            [
                "worker_handoff_attempt_0001",
                "correspondence_result_0_attempt_0001",
                "correspondence_result_1_attempt_0001",
                "nl_proof_target_node_2_attempt_0001",
                "reviewer_decision_attempt_0001",
            ],
        )
