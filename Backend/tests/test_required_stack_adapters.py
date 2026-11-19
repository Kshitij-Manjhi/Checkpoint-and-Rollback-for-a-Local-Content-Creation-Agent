import hashlib
import json
import os
import shutil
import pytest

from agent_runtime.artifact_store import GitArtifactStore
from agent_runtime.mysql_store import MySQLCheckpointStore


def test_git_artifact_store_versions_and_reads_checkpoint(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('Git executable is not installed')
    store = GitArtifactStore(tmp_path / 'artifact-repository')
    state = {'task_id': 't1', 'draft': 'version one'}
    canonical = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    uri = store.put('cp-1', state, digest)
    saved = store.get('cp-1')
    assert uri.startswith('git://')
    assert saved['state'] == state
    assert saved['sha256'] == digest
    revised = {'task_id': 't1', 'draft': 'version two', 'approval_status': 'approved'}
    revised_canonical = json.dumps(revised, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    revised_digest = hashlib.sha256(revised_canonical.encode()).hexdigest()
    store.put('cp-1', revised, revised_digest)
    assert store.get('cp-1')['state']['draft'] == 'version two'
    assert store.get('cp-1')['sha256'] == revised_digest


def test_mysql_mode_requires_explicit_connection_string(monkeypatch):
    monkeypatch.delenv('AGENT_RUNTIME_MYSQL_URL', raising=False)
    with pytest.raises(RuntimeError, match='AGENT_RUNTIME_MYSQL_URL'):
        MySQLCheckpointStore()


def test_chroma_evidence_index_persists_and_is_task_scoped(tmp_path):
    pytest.importorskip('chromadb')
    from agent_runtime.retrieval_store import LocalEvidenceIndex
    index = LocalEvidenceIndex(tmp_path / 'chroma')
    index.add('Draft about safe checkpoint recovery', task_id='task-a', kind='draft', reference_id='cp-a')
    index.add('Unrelated notes about cooking', task_id='task-b', kind='draft', reference_id='cp-b')
    matches = index.query('checkpoint recovery', task_id='task-a')
    assert matches and matches[0]['id'] == 'cp-a'
    reopened = LocalEvidenceIndex(tmp_path / 'chroma')
    assert reopened.query('checkpoint recovery', task_id='task-a')[0]['id'] == 'cp-a'
    assert reopened.query('checkpoint recovery', task_id='task-b') == []


def test_workflow_records_retrieved_context_in_checkpoint_state(tmp_path):
    from agent_runtime import CheckpointManager, ContentWorkflow, QualityPolicyEvaluator
    from agent_runtime.store import SQLiteCheckpointStore

    class FakeIndex:
        def __init__(self): self.added = []
        def query(self, text, *, task_id, limit=3):
            return [{'id': 'evidence-1', 'kind': 'draft', 'text': 'Earlier approved material about checkpoint integrity.', 'score': 0.8}]
        def add(self, text, *, task_id, kind, reference_id=None):
            self.added.append({'text': text, 'kind': kind, 'reference_id': reference_id})
            return reference_id or 'generated-id'

    index = FakeIndex()
    workflow = ContentWorkflow(CheckpointManager(SQLiteCheckpointStore(tmp_path / 'runtime.sqlite3')),
                               QualityPolicyEvaluator(min_characters=40), retrieval=index)
    result = workflow.generate('retrieval-task', 'Explain checkpoint integrity', mode='no_llm')
    assert result['state']['retrieved_evidence'][0]['id'] == 'evidence-1'
    assert {item['kind'] for item in index.added} == {'prompt', 'draft'}
