import hashlib
import json
import os
import shutil
from pathlib import Path
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
    assert len(uri) in (40, 64)
    assert saved['state'] == state
    assert saved['sha256'] == digest
    revised = {'task_id': 't1', 'draft': 'version two', 'approval_status': 'approved'}
    revised_canonical = json.dumps(revised, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    revised_digest = hashlib.sha256(revised_canonical.encode()).hexdigest()
    revised_commit = store.put('cp-1', revised, revised_digest)
    assert revised_commit != uri
    assert store.get('cp-1')['state']['draft'] == 'version two'
    assert store.get('cp-1')['sha256'] == revised_digest
    assert store.get('cp-1', uri)['state'] == state
    assert store.get('cp-1', revised_commit)['state'] == revised
    assert store.get('cp-1', '0' * 40) is None
    health = store.health_check()
    assert health['repository_ready'] is True
    assert health['has_commits'] is True
    assert health['head'] == revised_commit


def test_git_checkpoint_rollback_restores_exact_approved_content_and_hash(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('Git executable is not installed')
    from agent_runtime import CheckpointManager
    from agent_runtime.store import SQLiteCheckpointStore

    metadata = SQLiteCheckpointStore(tmp_path / 'metadata.sqlite3')
    artifacts = GitArtifactStore(tmp_path / 'artifact-repository')
    manager = CheckpointManager(metadata, artifact_store=artifacts)
    pending_state = {
        'task_id': 'e2e/task', 'draft': 'Approved content: exact bytes matter.\nSecond line.',
        'version': 1, 'approval_status': 'pending',
    }
    approved = manager.create_checkpoint('e2e/task', pending_state, is_safe=False)
    manager.review_checkpoint(
        approved.checkpoint_id, approved=True, reviewer='e2e-reviewer',
        reason='Verified exact-content rollback case.', reviewed_at='2026-11-20T10:00:00Z',
    )
    approved_row = metadata.get_checkpoint(approved.checkpoint_id)
    approved_state = approved_row['state']
    approved_commit = approved_row['artifact_commit_hash']
    approved_digest = approved_row['snapshot_sha256']
    assert approved_row['is_safe'] is True
    assert approved_commit
    first_restore = artifacts.restore(approved.checkpoint_id, approved_commit, 'e2e/task', approved_digest)
    working_path = first_restore['path']

    # Simulate an artifact edit and a later, unapproved checkpoint.
    (tmp_path / 'artifact-repository' / 'working' / Path(working_path).name).write_text(
        '{"draft":"unapproved modified content"}\n', encoding='utf-8'
    )
    manager.create_checkpoint('e2e/task', {
        'task_id': 'e2e/task', 'draft': 'Unapproved modified content', 'version': 2,
    }, is_safe=False, parent_checkpoint_id=approved.checkpoint_id)

    restored = manager.restore_latest_safe('e2e/task')
    restored_bytes = Path(working_path).read_bytes()
    expected_bytes = (json.dumps(approved_state, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    assert restored.checkpoint.checkpoint_id == approved.checkpoint_id
    assert restored.state == approved_state
    assert restored_bytes == expected_bytes
    assert hashlib.sha256(restored_bytes.rstrip(b'\n')).hexdigest() == approved_digest
    events = metadata.get_audit_events('e2e/task')
    rollback = next(event for event in events if event['event_type'] == 'ROLLBACK_VERIFIED')
    assert rollback['details']['artifact_commit_hash'] == approved_commit
    assert rollback['details']['restored_artifact']['sha256'] == approved_digest


def test_git_commit_failure_does_not_create_checkpoint_and_is_audited(tmp_path, monkeypatch):
    from agent_runtime import CheckpointManager
    from agent_runtime.store import SQLiteCheckpointStore

    metadata = SQLiteCheckpointStore(tmp_path / 'metadata.sqlite3')
    artifacts = GitArtifactStore(tmp_path / 'artifact-repository')
    manager = CheckpointManager(metadata, artifact_store=artifacts)
    monkeypatch.setattr(artifacts, 'put', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('commit failed')))
    with pytest.raises(RuntimeError, match='commit failed'):
        manager.create_checkpoint('failed-commit', {'draft': 'not stored'}, is_safe=True)
    assert metadata.list_checkpoints('failed-commit') == []
    assert metadata.get_audit_events('failed-commit')[-1]['event_type'] == 'ARTIFACT_COMMIT_FAILED'


def test_rollback_skips_missing_git_reference_and_fails_closed_if_none_valid(tmp_path):
    from agent_runtime import CheckpointManager, SnapshotIntegrityError
    from agent_runtime.store import SQLiteCheckpointStore

    metadata = SQLiteCheckpointStore(tmp_path / 'metadata.sqlite3')
    artifacts = GitArtifactStore(tmp_path / 'artifact-repository')
    manager = CheckpointManager(metadata, artifact_store=artifacts)
    first = manager.create_checkpoint('refs', {'draft': 'older approved'}, is_safe=True)
    second = manager.create_checkpoint('refs', {'draft': 'newer approved'}, is_safe=True)
    with metadata._connect() as connection:
        connection.execute('UPDATE checkpoints SET artifact_commit_hash=? WHERE checkpoint_id=?', ('0' * 40, second.checkpoint_id))
    restored = manager.restore_latest_safe('refs')
    assert restored.checkpoint.checkpoint_id == first.checkpoint_id
    assert any(event['event_type'] == 'ROLLBACK_CANDIDATE_REJECTED' and event['checkpoint_id'] == second.checkpoint_id
               for event in metadata.get_audit_events('refs'))
    with metadata._connect() as connection:
        connection.execute('UPDATE checkpoints SET artifact_commit_hash=? WHERE checkpoint_id=?', ('0' * 40, first.checkpoint_id))
    with pytest.raises(SnapshotIntegrityError):
        manager.restore_latest_safe('refs')


def test_git_artifact_with_corrupt_payload_is_not_readable(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('Git executable is not installed')
    store = GitArtifactStore(tmp_path / 'artifact-repository')
    state = {'draft': 'valid snapshot'}
    canonical = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    store.put('cp-corrupt', state, digest)
    path = tmp_path / 'artifact-repository' / 'artifacts' / 'cp-corrupt.json'
    path.write_text('{not valid json', encoding='utf-8')
    store._run('add', '--', 'artifacts/cp-corrupt.json')
    store._run('commit', '--only', '-m', 'corrupt fixture', '--', 'artifacts/cp-corrupt.json')
    corrupt_commit = store._run('rev-parse', '--verify', 'HEAD^{commit}')
    assert store.get('cp-corrupt', corrupt_commit) is None


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
    health = reopened.health_check()
    assert health['adapter'] == 'chromadb-persistent'
    assert health['collection'] == 'draftguard_evidence'
    assert health['document_count'] == 2


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
    assert result['state']['execution_context']['user_prompt'] == 'Explain checkpoint integrity'
    assert result['state']['execution_context']['retrieved_items'][0]['id'] == 'evidence-1'
    assert result['state']['agent_plan']['status'] == 'not_generated'
    assert result['state']['execution_context']['conversation_history_included'] is False
    assert {item['kind'] for item in index.added} == {'prompt', 'draft'}


def test_chroma_query_diagnostics_show_candidate_filter_counts(tmp_path):
    pytest.importorskip('chromadb')
    from agent_runtime.retrieval_store import LocalEvidenceIndex
    index = LocalEvidenceIndex(tmp_path / 'chroma-diagnostics', min_score=0.2)
    index.add('DraftGuard stores checkpoint artifacts in Git and MySQL records audit metadata.',
              task_id='task-a', kind='evidence', reference_id='source-1')
    result = index.query_detailed('DraftGuard stores checkpoint artifacts in Git.', task_id='task-a', limit=5)
    assert result['diagnostics']['candidate_count'] == 1
    assert result['diagnostics']['eligible_passage_count'] == 1
    assert result['items'][0]['id'] == 'source-1'
    assert result['diagnostics']['candidates'][0]['id'] == 'source-1'
