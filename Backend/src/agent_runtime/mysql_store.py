"""MySQL metadata adapter. Configure AGENT_RUNTIME_MYSQL_URL; no silent fallback."""
from __future__ import annotations
import hashlib, json, os
from typing import Any
from urllib.parse import urlparse, unquote

class _Connection:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, typ, value, tb):
        self.conn.commit() if typ is None else self.conn.rollback()
        self.conn.close()
    def execute(self, sql, params=()):
        cursor = self.conn.cursor(dictionary=True)
        cursor.execute(sql.replace('?', '%s'), params)
        return cursor

class MySQLCheckpointStore:
    """Implements the CheckpointStore contract against MySQL 8+ metadata tables."""
    def __init__(self, url: str | None = None):
        url = url or os.environ.get('AGENT_RUNTIME_MYSQL_URL')
        if not url: raise RuntimeError('Set AGENT_RUNTIME_MYSQL_URL when AGENT_RUNTIME_STORAGE=mysql.')
        try: import mysql.connector
        except ImportError as exc: raise RuntimeError('Install mysql-connector-python to use MySQL metadata.') from exc
        parsed = urlparse(url)
        if parsed.scheme not in ('mysql', 'mysql+mysqlconnector') or not parsed.hostname or not parsed.path.strip('/'):
            raise ValueError('URL format: mysql://user:password@host:3306/database')
        self._connector = mysql.connector
        self._config = {'host': parsed.hostname, 'port': parsed.port or 3306, 'user': unquote(parsed.username or ''), 'password': unquote(parsed.password or ''), 'database': parsed.path.strip('/'), 'autocommit': False}
        self._initialize()
    def _connect(self): return _Connection(self._connector.connect(**self._config))
    def health_check(self):
        """Verify connectivity and the required metadata tables without writing."""
        with self._connect() as c:
            row = c.execute('SELECT 1 AS ok').fetchone()
            required = {"tasks", "checkpoints", "audit_events", "evaluation_runs"}
            tables = {str(item.get("Tables_in_" + self._config["database"], ""))
                      for item in c.execute("SHOW TABLES").fetchall()}
        if not row or int(row.get('ok', 0)) != 1:
            raise RuntimeError('MySQL health query returned an unexpected result')
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError('Required MySQL tables are missing: ' + ', '.join(missing))
        return {'adapter': 'mysql', 'schema_ready': True}
    def _initialize(self):
        statements = [
            "CREATE TABLE IF NOT EXISTS checkpoints (checkpoint_id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(255) NOT NULL, sequence INT NOT NULL, parent_checkpoint_id VARCHAR(36) NULL, snapshot_json LONGTEXT NOT NULL, snapshot_sha256 CHAR(64) NOT NULL, artifact_commit_hash VARCHAR(64) NULL, is_safe TINYINT NOT NULL, created_at VARCHAR(40) NOT NULL, UNIQUE KEY uq_task_sequence(task_id,sequence), KEY idx_task_safe(task_id,is_safe,sequence))",
            "CREATE TABLE IF NOT EXISTS tasks (task_id VARCHAR(255) PRIMARY KEY, title VARCHAR(500) NOT NULL, created_at VARCHAR(40) NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit_events (event_id BIGINT AUTO_INCREMENT PRIMARY KEY, task_id VARCHAR(255) NOT NULL, event_type VARCHAR(100) NOT NULL, checkpoint_id VARCHAR(36) NULL, details_json LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, KEY idx_audit_task(task_id,event_id))"
            ,"CREATE TABLE IF NOT EXISTS evaluation_runs (experiment_id VARCHAR(36) PRIMARY KEY, started_at VARCHAR(40) NOT NULL, status VARCHAR(32) NOT NULL, report_json LONGTEXT NOT NULL, KEY idx_evaluation_started(started_at))"
        ]
        with self._connect() as c:
            for statement in statements: c.execute(statement)
            columns = c.execute("SHOW COLUMNS FROM checkpoints LIKE 'artifact_commit_hash'").fetchall()
            if not columns:
                c.execute("ALTER TABLE checkpoints ADD COLUMN artifact_commit_hash VARCHAR(64) NULL")
    def insert_checkpoint(self, record: dict[str, Any]) -> int:
        with self._connect() as c:
            next_row = c.execute('SELECT COALESCE(MAX(sequence),0)+1 AS n FROM checkpoints WHERE task_id=? FOR UPDATE', (record['task_id'],)).fetchone()
            record['sequence'] = int(next_row['n'])
            c.execute('INSERT INTO checkpoints(checkpoint_id,task_id,sequence,parent_checkpoint_id,snapshot_json,snapshot_sha256,artifact_commit_hash,is_safe,created_at) VALUES(?,?,?,?,?,?,?,?,?)', (record['checkpoint_id'],record['task_id'],record['sequence'],record['parent_checkpoint_id'],record['snapshot_json'],record['snapshot_sha256'],record.get('artifact_commit_hash'),int(record['is_safe']),record['created_at']))
            c.execute('INSERT INTO audit_events(task_id,event_type,checkpoint_id,details_json,created_at) VALUES(?,?,?,?,?)', (record['task_id'],'CHECKPOINT_CREATED',record['checkpoint_id'],json.dumps({'safe':record['is_safe'],'sequence':record['sequence'],'artifact_commit_hash':record.get('artifact_commit_hash')},sort_keys=True),record['created_at']))
        return record['sequence']
    def create_task(self, task_id, title, created_at):
        with self._connect() as c:
            existing = c.execute('SELECT task_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if existing: return False
            c.execute('INSERT INTO tasks(task_id,title,created_at) VALUES(?,?,?)', (task_id,title,created_at)); return True
    def get_task(self, task_id):
        with self._connect() as c: return c.execute('SELECT task_id,title,created_at FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    def list_safe_checkpoints(self, task_id):
        with self._connect() as c: return c.execute('SELECT * FROM checkpoints WHERE task_id=? AND is_safe=1 ORDER BY sequence DESC',(task_id,)).fetchall()
    def latest_safe_checkpoint(self, task_id):
        with self._connect() as c: return c.execute('SELECT * FROM checkpoints WHERE task_id=? AND is_safe=1 ORDER BY sequence DESC LIMIT 1',(task_id,)).fetchone()
    def add_audit_event(self, task_id, event_type, checkpoint_id, details, created_at):
        with self._connect() as c: c.execute('INSERT INTO audit_events(task_id,event_type,checkpoint_id,details_json,created_at) VALUES(?,?,?,?,?)',(task_id,event_type,checkpoint_id or None,json.dumps(details,sort_keys=True),created_at))
    def inject_artifact_reference_fault(self, checkpoint_id, commit_hash):
        """Synthetic experiment hook: point a trial checkpoint at a faulty Git ref."""
        with self._connect() as c:
            c.execute('UPDATE checkpoints SET artifact_commit_hash=? WHERE checkpoint_id=?',(commit_hash,checkpoint_id))
    def save_evaluation_run(self, report):
        with self._connect() as c:
            c.execute('INSERT INTO evaluation_runs(experiment_id,started_at,status,report_json) VALUES(?,?,?,?)',
                      (report['experiment_id'],report['started_at'],report['status'],json.dumps(report,ensure_ascii=False,sort_keys=True)))
    def list_evaluation_runs(self, limit=50):
        with self._connect() as c:
            rows=c.execute('SELECT experiment_id,started_at,status,report_json FROM evaluation_runs ORDER BY started_at DESC LIMIT ?',
                           (max(1,min(int(limit),200)),)).fetchall()
        return [{'experiment_id':r['experiment_id'],'started_at':r['started_at'],'status':r['status'],'report':json.loads(r['report_json'])} for r in rows]
    def get_evaluation_run(self, experiment_id):
        with self._connect() as c:
            row=c.execute('SELECT report_json FROM evaluation_runs WHERE experiment_id=?',(experiment_id,)).fetchone()
        return json.loads(row['report_json']) if row else None
    def get_audit_events(self, task_id):
        with self._connect() as c: rows=c.execute('SELECT * FROM audit_events WHERE task_id=? ORDER BY event_id',(task_id,)).fetchall()
        return [{'event_id':r['event_id'],'task_id':r['task_id'],'event_type':r['event_type'],'checkpoint_id':r['checkpoint_id'],'details':json.loads(r['details_json']),'created_at':r['created_at']} for r in rows]
    def review_checkpoint(self, checkpoint_id, *, approved, reviewer, reason, reviewed_at, artifact_commit_hash=None):
        with self._connect() as c:
            row=c.execute('SELECT * FROM checkpoints WHERE checkpoint_id=? FOR UPDATE',(checkpoint_id,)).fetchone()
            if row is None: return None
            try: state=json.loads(row['snapshot_json'])
            except json.JSONDecodeError as exc: raise ValueError('Cannot review invalid snapshot JSON') from exc
            state.update({'approval_status':'approved' if approved else 'rejected','reviewer':reviewer,'review_reason':reason,'reviewed_at':reviewed_at})
            snapshot=json.dumps(state,sort_keys=True,separators=(',',':'),ensure_ascii=False); digest=hashlib.sha256(snapshot.encode()).hexdigest()
            c.execute('UPDATE checkpoints SET snapshot_json=?,snapshot_sha256=?,artifact_commit_hash=COALESCE(?,artifact_commit_hash),is_safe=? WHERE checkpoint_id=?',(snapshot,digest,artifact_commit_hash,int(approved),checkpoint_id))
            c.execute('INSERT INTO audit_events(task_id,event_type,checkpoint_id,details_json,created_at) VALUES(?,?,?,?,?)',(row['task_id'],'HUMAN_REVIEW_APPROVED' if approved else 'HUMAN_REVIEW_REJECTED',checkpoint_id,json.dumps({'reviewer':reviewer,'reason':reason,'approval_status':state['approval_status'],'is_safe':approved,'artifact_commit_hash':artifact_commit_hash},sort_keys=True),reviewed_at))
            return {'checkpoint_id':checkpoint_id,'task_id':row['task_id'],'is_safe':approved,'approval_status':state['approval_status'],'reviewer':reviewer,'review_reason':reason,'reviewed_at':reviewed_at}
    def tamper_snapshot_for_test(self, checkpoint_id, snapshot_json):
        with self._connect() as c: c.execute('UPDATE checkpoints SET snapshot_json=? WHERE checkpoint_id=?',(snapshot_json,checkpoint_id))
    def list_checkpoints(self, task_id):
        with self._connect() as c: rows=c.execute('SELECT * FROM checkpoints WHERE task_id=? ORDER BY sequence',(task_id,)).fetchall()
        return [{'checkpoint_id':r['checkpoint_id'],'task_id':r['task_id'],'sequence':r['sequence'],'parent_checkpoint_id':r['parent_checkpoint_id'],'snapshot_sha256':r['snapshot_sha256'],'artifact_commit_hash':r['artifact_commit_hash'],'is_safe':bool(r['is_safe']),'created_at':r['created_at']} for r in rows]
    def get_checkpoint(self, checkpoint_id):
        with self._connect() as c: row=c.execute('SELECT * FROM checkpoints WHERE checkpoint_id=?',(checkpoint_id,)).fetchone()
        if row is None: return None
        valid=hashlib.sha256(row['snapshot_json'].encode()).hexdigest()==row['snapshot_sha256']; state=None
        if valid:
            try: state=json.loads(row['snapshot_json'])
            except json.JSONDecodeError: valid=False
        return {'checkpoint_id':row['checkpoint_id'],'task_id':row['task_id'],'sequence':row['sequence'],'parent_checkpoint_id':row['parent_checkpoint_id'],'snapshot_sha256':row['snapshot_sha256'],'artifact_commit_hash':row['artifact_commit_hash'],'is_safe':bool(row['is_safe']),'created_at':row['created_at'],'integrity_verified':valid,'state':state}
