"""Git-backed checkpoint artifacts with verified, non-destructive restoration."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping


def canonical_json(state: Mapping[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class GitArtifactStore:
    """Commit immutable checkpoint snapshots and restore by copying a verified tree object.

    No branch reset, checkout, push, or force push is used. The live working copy is
    written atomically under ``working/`` so rollback never rewrites Git history.
    """

    def __init__(self, repository_path: str | Path):
        self.repository_path = Path(repository_path).expanduser().resolve()
        self.repository_path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not (self.repository_path / ".git").exists():
            self._run("init")
        # Windows Git otherwise rejects object paths nested under long project
        # or pytest temporary directories, even when artifact names are short.
        if os.name == "nt":
            self._run("config", "core.longpaths", "true")
        # Set identity locally so this repository does not depend on global Git config.
        if not self._run("config", "--get", "user.email", check=False):
            self._run("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "draftguard@localhost"))
        if not self._run("config", "--get", "user.name", check=False):
            self._run("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "DraftGuard"))

    def _run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository_path), *args],
            text=True, capture_output=True, encoding="utf-8",
        )
        if check and result.returncode:
            raise RuntimeError(f"Git artifact store failed ({' '.join(args)}): {result.stderr.strip()}")
        return result.stdout.strip()

    def health_check(self) -> dict[str, Any]:
        """Verify the configured repository is readable without changing it."""
        if self._run('rev-parse', '--is-inside-work-tree') != 'true':
            raise RuntimeError('Configured Git path is not a work tree')
        try:
            head = self._run('rev-parse', '--verify', 'HEAD^{commit}')
        except RuntimeError:
            head = None
        return {'adapter': 'git', 'repository_ready': True,
                'has_commits': head is not None, 'head': head}

    @staticmethod
    def _artifact_path(checkpoint_id: str) -> str:
        if not checkpoint_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in checkpoint_id):
            raise ValueError("Invalid checkpoint ID for artifact path")
        return f"artifacts/{checkpoint_id}.json"

    def _read_blob_at_commit(self, commit_hash: str, rel: str) -> str | None:
        commit = self._run("rev-parse", "--verify", f"{commit_hash}^{{commit}}")
        tree = self._run("rev-parse", "--verify", f"{commit}^{{tree}}")
        entries = self._run("ls-tree", "--full-tree", tree, "--", rel).splitlines()
        matching = [line for line in entries if line.partition("\t")[2] == rel]
        if len(matching) != 1:
            return None
        fields = matching[0].partition("\t")[0].split()
        if len(fields) != 3 or fields[1] != "blob":
            return None
        return self._run("cat-file", "blob", fields[2])

    def put(self, checkpoint_id: str, state: dict[str, Any], digest: str) -> str:
        """Commit a checkpoint snapshot and return its immutable commit hash."""
        rel = self._artifact_path(checkpoint_id)
        canonical = canonical_json(state)
        actual_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual_digest != digest:
            raise ValueError("Snapshot digest does not match state being committed")
        payload = json.dumps(
            {"checkpoint_id": checkpoint_id, "sha256": digest, "state": state},
            sort_keys=True, ensure_ascii=False, indent=2,
        ) + "\n"
        with self._lock:
            path = self.repository_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8", newline="\n")
            self._run("add", "--", rel)
            staged = self._run("diff", "--cached", "--name-only", "--", rel)
            if not staged:
                raise RuntimeError(f"Git did not stage checkpoint artifact {checkpoint_id}")
            self._run("commit", "--only", "-m", f"checkpoint {checkpoint_id}", "--", rel)
            commit_hash = self._run("rev-parse", "--verify", "HEAD^{commit}")
            if self._read_blob_at_commit(commit_hash, rel) is None:
                raise RuntimeError(f"Git commit {commit_hash} does not contain {rel}")
            return commit_hash

    def get(self, checkpoint_id: str, commit_hash: str | None = None) -> dict[str, Any] | None:
        """Read the artifact at a recorded commit; HEAD is used only for compatibility."""
        rel = self._artifact_path(checkpoint_id)
        ref = commit_hash or "HEAD"
        if ref != "HEAD" and (len(ref) not in (40, 64) or any(c not in "0123456789abcdefABCDEF" for c in ref)):
            return None
        try:
            payload = self._read_blob_at_commit(ref, rel)
            if payload is None:
                return None
            value = json.loads(payload)
        except (RuntimeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("checkpoint_id") != checkpoint_id:
            return None
        state = value.get("state")
        if not isinstance(state, dict):
            return None
        actual_digest = hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()
        if value.get("sha256") != actual_digest:
            return None
        return {"checkpoint_id": checkpoint_id, "sha256": actual_digest, "state": state}

    def restore(self, checkpoint_id: str, commit_hash: str | None, task_id: str, expected_digest: str) -> dict[str, Any]:
        """Atomically materialize a verified snapshot without changing Git refs/history."""
        if not commit_hash:
            raise ValueError("Checkpoint has no recorded Git commit hash")
        value = self.get(checkpoint_id, commit_hash)
        if value is None or value["sha256"] != expected_digest:
            raise ValueError("Recorded Git artifact is missing, corrupt, or has a mismatched digest")
        if not task_id:
            raise ValueError("Task ID is required for restored artifact path")
        working_dir = self.repository_path / "working"
        working_dir.mkdir(parents=True, exist_ok=True)
        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        target = working_dir / f"{task_key}.json"
        data = (canonical_json(value["state"]) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{task_key}-", suffix=".tmp", dir=working_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        restored_digest = hashlib.sha256(canonical_json(value["state"]).encode("utf-8")).hexdigest()
        if restored_digest != expected_digest:
            raise ValueError("Restored artifact failed post-write digest validation")
        return {"path": str(target), "sha256": restored_digest, "commit_hash": commit_hash}
