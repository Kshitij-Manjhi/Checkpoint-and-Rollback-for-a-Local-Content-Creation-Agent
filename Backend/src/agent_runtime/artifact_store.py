"""Git-backed immutable artifact snapshots for DraftGuard checkpoints."""
from __future__ import annotations
import json, os, subprocess
from pathlib import Path
from typing import Any

class GitArtifactStore:
    """Stores each checkpoint state as a committed JSON artifact in a dedicated repo."""
    def __init__(self, repository_path: str | Path):
        self.repository_path = Path(repository_path).expanduser().resolve()
        self.repository_path.mkdir(parents=True, exist_ok=True)
        if not (self.repository_path / '.git').exists():
            self._run('init')
            self._run('config', 'user.email', os.environ.get('GIT_AUTHOR_EMAIL', 'draftguard@localhost'))
            self._run('config', 'user.name', os.environ.get('GIT_AUTHOR_NAME', 'DraftGuard'))
    def _run(self, *args: str) -> str:
        result = subprocess.run(['git', '-C', str(self.repository_path), *args], text=True, capture_output=True)
        if result.returncode: raise RuntimeError(f'Git artifact store failed: {result.stderr.strip()}')
        return result.stdout.strip()
    def put(self, checkpoint_id: str, state: dict[str, Any], digest: str) -> str:
        rel = Path('artifacts') / f'{checkpoint_id}.json'
        path = self.repository_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'checkpoint_id': checkpoint_id, 'sha256': digest, 'state': state}, sort_keys=True, ensure_ascii=False, indent=2), encoding='utf-8')
        self._run('add', str(rel))
        if self._run('status', '--porcelain'): self._run('commit', '-m', f'checkpoint {checkpoint_id}')
        commit = self._run('rev-parse', 'HEAD')
        return f'git://{self.repository_path.as_posix()}@{commit}/{rel.as_posix()}'
    def get(self, checkpoint_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._run('show', f'HEAD:artifacts/{checkpoint_id}.json'))
            return value if value.get('checkpoint_id') == checkpoint_id else None
        except (RuntimeError, json.JSONDecodeError): return None
