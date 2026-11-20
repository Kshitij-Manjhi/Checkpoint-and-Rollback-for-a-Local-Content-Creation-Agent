"""Persistent Chroma retrieval using deterministic local feature hashing (no model downloads)."""
from __future__ import annotations
import hashlib, math, re
from pathlib import Path
from typing import Any

class LocalEvidenceIndex:
    """Stores task-scoped prompts and drafts in Chroma with locally computed embeddings."""
    def __init__(
        self,
        directory: str | Path,
        dimensions: int = 384,
        min_score: float = 0.5,
    ):
        """Create a persistent index.

        ``min_score`` filters out weak nearest-neighbour results. With the compact
        feature-hashing embeddings used here, a moderate threshold is needed to
        avoid treating hash collisions as semantic matches. Chroma returns the nearest item even when
        every item is unrelated, so task filtering alone is not sufficient.
        """
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0.0 and 1.0")
        try: import chromadb
        except ImportError as exc: raise RuntimeError('Install chromadb to enable local evidence retrieval.') from exc
        self.directory = Path(directory).expanduser().resolve(); self.directory.mkdir(parents=True, exist_ok=True)
        self.dimensions = dimensions
        self.min_score = min_score
        self.client = chromadb.PersistentClient(path=str(self.directory))
        self.collection = self.client.get_or_create_collection(name='draftguard_evidence', metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    def health_check(self) -> dict[str, Any]:
        """Check that the persistent Chroma collection can answer a metadata read."""
        return {'adapter': 'chromadb-persistent', 'collection': self.collection.name,
                'document_count': int(self.collection.count())}
    def _embed(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        features = tokens + [f'{a}_{b}' for a,b in zip(tokens, tokens[1:])]
        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.blake2b(feature.encode('utf-8'), digest_size=8).digest()
            index = int.from_bytes(digest[:4], 'little') % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(v*v for v in vector))
        return [v/norm for v in vector] if norm else vector
    def add(self, text: str, *, task_id: str, kind: str, reference_id: str | None = None) -> str:
        clean=text.strip()
        if not clean: return ''
        doc_id=reference_id or f'{task_id}:{kind}:{self.collection.count()+1}'
        existing=self.collection.get(ids=[doc_id])
        if existing.get('ids'): return doc_id
        self.collection.add(ids=[doc_id], documents=[clean], metadatas=[{'task_id':task_id,'kind':kind}], embeddings=[self._embed(clean)])
        return doc_id
    def query(self, text: str, *, task_id: str, limit: int = 3) -> list[dict[str, Any]]:
        if not text.strip() or not self.collection.count(): return []
        result=self.collection.query(query_embeddings=[self._embed(text)], n_results=max(limit, 1), where={'task_id':task_id}, include=['documents','metadatas','distances'])
        found=[]
        for doc_id, document, metadata, distance in zip(
            result['ids'][0],
            result['documents'][0],
            result['metadatas'][0],
            result['distances'][0],
        ):
            score = 1.0 - float(distance)
            # Chroma returns the closest record even if it is unrelated.
            # Filter weak matches so an unrelated task-local document does not
            # get presented as useful evidence.
            if score < self.min_score:
                continue
            found.append({
                'id': doc_id,
                'text': document,
                'kind': metadata.get('kind', 'evidence'),
                'score': score,
            })
        return found[:limit]
