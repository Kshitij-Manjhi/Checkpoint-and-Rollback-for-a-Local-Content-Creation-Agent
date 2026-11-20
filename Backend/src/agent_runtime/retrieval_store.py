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
        min_score: float = 0.3,
    ):
        """Create a persistent index.

        ``min_score`` filters out weak nearest-neighbour results. With the compact
        feature-hashing embeddings used here, 0.30 retrieves relevant claim-level
        passages from the demo corpus while excluding its unrelated France query
        (0.12). Chroma returns the nearest item even when every item is unrelated,
        so task filtering alone is not sufficient.
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
    @staticmethod
    def _query_terms(text: str) -> set[str]:
        stop_words = {"about", "after", "again", "and", "are", "before", "but", "can",
                      "does", "each", "for", "from", "how", "into", "its", "not", "of",
                      "the", "this", "that", "then", "there", "these", "they", "through",
                      "during", "using", "uses", "what", "when", "where", "which", "while",
                      "with", "would", "your"}
        return {token for token in re.findall(r"[a-zA-Z0-9_]+", text.lower())
                if len(token) > 2 and token not in stop_words}
    def add(self, text: str, *, task_id: str, kind: str, reference_id: str | None = None) -> str:
        clean=text.strip()
        if not clean: return ''
        doc_id=reference_id or f'{task_id}:{kind}:{self.collection.count()+1}'
        existing=self.collection.get(ids=[doc_id], include=['documents','metadatas'])
        if existing.get('ids'):
            old_text=(existing.get('documents') or [None])[0]
            old_metadata=(existing.get('metadatas') or [{}])[0] or {}
            if old_text != clean or old_metadata.get('task_id') != task_id or old_metadata.get('kind') != kind:
                raise ValueError(f'Evidence reference {doc_id!r} already exists with different content or scope')
            return doc_id
        self.collection.add(ids=[doc_id], documents=[clean], metadatas=[{'task_id':task_id,'kind':kind}], embeddings=[self._embed(clean)])
        return doc_id
    def query(self, text: str, *, task_id: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.query_detailed(text, task_id=task_id, limit=limit)["items"]

    def query_detailed(self, text: str, *, task_id: str, limit: int = 3) -> dict[str, Any]:
        """Return thresholded passages plus safe counts/IDs for retrieval diagnostics."""
        if not text.strip() or not self.collection.count():
            return {"items": [], "diagnostics": {"candidate_count": 0, "eligible_passage_count": 0,
                    "filtered_by_score_count": 0, "filtered_empty_count": 0,
                    "filtered_by_lexical_count": 0, "filtered_by_kind_count": 0,
                    "min_score": self.min_score, "candidates": []}}
        result=self.collection.query(query_embeddings=[self._embed(text)],
            n_results=min(max(limit, 10), int(self.collection.count())), where={'task_id':task_id},
            include=['documents','metadatas','distances'])
        found=[]; candidates=[]; below_score=empty=bad_kind=lexically_unrelated=0
        query_terms = self._query_terms(text)
        for doc_id, document, metadata, distance in zip(
            result['ids'][0],
            result['documents'][0],
            result['metadatas'][0],
            result['distances'][0],
        ):
            score = 1.0 - float(distance)
            kind = metadata.get('kind', 'evidence')
            document_terms = self._query_terms(document or "")
            overlap = query_terms & document_terms
            lexical_coverage = len(overlap) / len(query_terms) if query_terms else 0.0
            candidates.append({"id": str(doc_id), "kind": str(kind), "score": round(score, 6),
                               "lexical_overlap_count": len(overlap),
                               "lexical_coverage": round(lexical_coverage, 6)})
            if score < self.min_score:
                below_score += 1
                continue
            # Feature hashing can create a high cosine score from bucket
            # collisions even when the query and passage share no meaningful
            # terms. Require lexical overlap as a second, inspectable gate.
            if not overlap:
                lexically_unrelated += 1
                continue
            if not isinstance(document, str) or not document.strip():
                empty += 1
                continue
            if kind not in {"evidence", "source", "reference", "document", "draft", "prompt"}:
                bad_kind += 1
                continue
            found.append({
                'id': doc_id,
                'text': document,
                'kind': kind,
                'score': score,
            })
        return {"items": found[:limit], "diagnostics": {
            "candidate_count": len(candidates), "eligible_passage_count": len(found[:limit]),
            "filtered_by_score_count": below_score, "filtered_empty_count": empty,
            "filtered_by_lexical_count": lexically_unrelated,
            "filtered_by_kind_count": bad_kind, "min_score": self.min_score,
            "candidates": candidates,
        }}
