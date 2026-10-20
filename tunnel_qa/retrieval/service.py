import hashlib
import math
import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from tunnel_qa.core.enums import ProcessingStatus, ReviewStatus
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.tracing import get_trace_id
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.knowledge.models import Evidence, KnowledgeEntity, KnowledgeEvidenceLink, KnowledgeRelation
from tunnel_qa.retrieval.schemas import RetrievalFilters, RetrievalHit, RetrievalResult


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = re.findall(r"[a-z0-9_]+", text)
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.append(sequence)
        tokens.extend(sequence[index:index + 2] for index in range(len(sequence) - 1))
        tokens.extend(char for char in sequence if char not in "的了和与及或是在应当")
    return [token for token in tokens if token]


def hashed_vector(text: str, dimensions: int = 256) -> dict[int, float]:
    counts: Counter[int] = Counter()
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % dimensions
        counts[index] += 1
    norm = math.sqrt(sum(value * value for value in counts.values()))
    if not norm:
        return {}
    return {index: value / norm for index, value in counts.items()}


def cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


class HybridRetrievalService:
    def __init__(
        self, session: Session, content_store: DocumentContentStore | None = None
    ) -> None:
        self.session = session
        self.content_store = content_store

    def search(
        self,
        query: str,
        access: AccessContext,
        *,
        filters: RetrievalFilters | None = None,
        top_k: int = 8,
    ) -> RetrievalResult:
        access.require("knowledge:read")
        query = query.strip()
        if not query:
            raise AppError("EMPTY_QUERY", "检索问题不能为空", field="question")
        if len(query) > 2000:
            raise AppError("QUERY_TOO_LONG", "检索问题长度超过限制", field="question")
        if top_k < 1 or top_k > 100:
            raise AppError("INVALID_TOP_K", "top_k 必须在 1 到 100 之间", field="top_k")
        filters = filters or RetrievalFilters()

        statement = (
            select(Chunk, SourceDocument)
            .join(SourceDocument, Chunk.document_id == SourceDocument.id)
            .where(SourceDocument.status == ProcessingStatus.PUBLISHED,
                   Chunk.processing_version == SourceDocument.processing_version)
        )
        if filters.project_site:
            statement = statement.where(SourceDocument.project_site.contains(filters.project_site))
        if filters.source_types:
            statement = statement.where(SourceDocument.source_type.in_(filters.source_types))
        if filters.versions:
            statement = statement.where(SourceDocument.version.in_(filters.versions))
        rows = [
            (chunk, document)
            for chunk, document in self.session.execute(statement).all()
            if access.can_access(chunk.security_level)
            and access.can_access(document.security_level)
        ]
        if not rows:
            return RetrievalResult(query=query, hits=[], trace_id=get_trace_id())

        stored_texts = (
            self.content_store.get_chunk_texts(chunk.id for chunk, _ in rows)
            if self.content_store
            else {}
        )
        chunk_texts = {
            chunk.id: stored_texts.get(chunk.id, chunk.text) for chunk, _ in rows
        }

        query_tokens = tokenize(query)
        query_counts = Counter(query_tokens)
        document_token_counts = {
            chunk.id: Counter(tokenize(chunk_texts[chunk.id])) for chunk, _ in rows
        }
        document_frequency: Counter[str] = Counter()
        for counts in document_token_counts.values():
            document_frequency.update(counts.keys())
        average_length = sum(sum(counts.values()) for counts in document_token_counts.values()) / len(rows)
        keyword_scores = {
            chunk.id: self._bm25(
                query_counts,
                document_token_counts[chunk.id],
                document_frequency,
                len(rows),
                average_length,
            )
            for chunk, _ in rows
        }

        query_vector = hashed_vector(query)
        semantic_scores = {
            chunk.id: cosine(query_vector, hashed_vector(chunk_texts[chunk.id]))
            for chunk, _ in rows
        }
        graph_scores, graph_paths = self._graph_signals(query, access)

        normalized_keyword = self._normalize(keyword_scores)
        normalized_semantic = self._normalize(semantic_scores)
        candidates: list[RetrievalHit] = []
        for chunk, document in rows:
            graph_score = graph_scores.get(chunk.id, 0.0)
            title_overlap = len(set(query_tokens) & set(tokenize(document.title)))
            rerank_score = min(1.0, title_overlap / max(1, len(set(query_tokens))))
            # 哈希向量可能发生偶然碰撞。语义分只能重排已有可解释命中的候选，
            # 不能在关键词、标题和图谱均无命中时单独使资料入选。
            if keyword_scores.get(chunk.id, 0.0) <= 0 and graph_score <= 0 and title_overlap == 0:
                continue
            score = (
                0.42 * normalized_keyword.get(chunk.id, 0.0)
                + 0.33 * normalized_semantic.get(chunk.id, 0.0)
                + 0.20 * graph_score
                + 0.05 * rerank_score
            )
            if score <= 0:
                continue
            metadata = document.metadata_extra if isinstance(document.metadata_extra, dict) else {}
            chunking = metadata.get("chunking")
            keep_complete = isinstance(chunking, dict) and chunking.get("chunk_model") == "key-v2"
            candidates.append(
                RetrievalHit(
                    chunk_id=chunk.id,
                    document_id=document.id,
                    source_id=document.source_id,
                    title=document.title,
                    source_type=document.source_type,
                    version=document.version,
                    effective_date=document.effective_date,
                    content_location=chunk.content_location,
                    # Key blocks preserve their conditions and exceptions as a
                    # unit. Cutting at 1000 characters can remove a late negation.
                    excerpt=chunk_texts[chunk.id] if keep_complete else chunk_texts[chunk.id][:1000],
                    keyword_score=round(keyword_scores.get(chunk.id, 0.0), 6),
                    semantic_score=round(semantic_scores.get(chunk.id, 0.0), 6),
                    graph_score=round(graph_score, 6),
                    rerank_score=round(rerank_score, 6),
                    score=round(score, 6),
                    graph_path=graph_paths.get(chunk.id, []),
                )
            )
        candidates.sort(key=lambda hit: (-hit.score, hit.source_id, hit.content_location))
        return RetrievalResult(query=query, hits=candidates[:top_k], trace_id=get_trace_id())

    @staticmethod
    def _bm25(
        query: Counter[str],
        document: Counter[str],
        document_frequency: Counter[str],
        document_count: int,
        average_length: float,
    ) -> float:
        score = 0.0
        document_length = sum(document.values())
        k1, b = 1.5, 0.75
        for term, query_frequency in query.items():
            frequency = document.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (
                1 - b + b * document_length / max(1.0, average_length)
            )
            score += inverse_frequency * frequency * (k1 + 1) / denominator * query_frequency
        return score

    def _graph_signals(
        self, query: str, access: AccessContext
    ) -> tuple[dict[str, float], dict[str, list[str]]]:
        chunk_scores: dict[str, float] = defaultdict(float)
        paths: dict[str, list[str]] = defaultdict(list)
        entities = self.session.scalars(
            select(KnowledgeEntity).where(KnowledgeEntity.status == ReviewStatus.PUBLISHED)
        ).all()
        matched = [
            entity
            for entity in entities
            if access.can_access(entity.security_level)
            and (entity.standard_name in query or query in entity.standard_name)
        ]
        visible_entities = {entity.id: entity for entity in entities
                            if access.can_access(entity.security_level)}
        matched_ids = {entity.id for entity in matched}
        relations = self.session.scalars(
            select(KnowledgeRelation).where(
                KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                (KnowledgeRelation.source_entity_id.in_(matched_ids))
                | (KnowledgeRelation.target_entity_id.in_(matched_ids)),
            )
        ).all() if matched_ids else []
        relations = [relation for relation in relations
                     if access.can_access(relation.security_level)
                     and relation.source_entity_id in visible_entities
                     and relation.target_entity_id in visible_entities]
        targets = {("entity", entity.id): entity for entity in matched}
        targets.update({("relation", relation.id): relation for relation in relations})
        evidence_targets: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for key, fact in targets.items():
            if fact.evidence_id:
                evidence_targets[fact.evidence_id].add(key)
        if targets:
            links = self.session.scalars(select(KnowledgeEvidenceLink).where(
                KnowledgeEvidenceLink.target_id.in_([key[1] for key in targets])
            )).all()
            for link in links:
                key = (link.target_type, link.target_id)
                if key in targets:
                    evidence_targets[link.evidence_id].add(key)
        source_rows = self.session.execute(
            select(Evidence, Chunk, SourceDocument)
            .join(Chunk, Evidence.chunk_id == Chunk.id)
            .join(SourceDocument, Chunk.document_id == SourceDocument.id)
            .where(Evidence.id.in_(evidence_targets),
                   SourceDocument.status == ProcessingStatus.PUBLISHED,
                   Chunk.processing_version == SourceDocument.processing_version)
        ).all() if evidence_targets else []
        for evidence, chunk, document in source_rows:
            if not access.can_access(chunk.security_level) or not access.can_access(document.security_level):
                continue
            for key in sorted(evidence_targets[evidence.id]):
                fact = targets[key]
                if key[0] == "entity":
                    score, path = 0.8, fact.standard_name
                else:
                    source = visible_entities[fact.source_entity_id]
                    target = visible_entities[fact.target_entity_id]
                    score = 1.0
                    path = f"{source.standard_name}→{fact.relation_type}→{target.standard_name}"
                chunk_scores[chunk.id] = max(chunk_scores[chunk.id], score)
                if path not in paths[chunk.id]:
                    paths[chunk.id].append(path)
        return dict(chunk_scores), dict(paths)

    @staticmethod
    def _normalize(scores: dict[str, float]) -> dict[str, float]:
        if not scores:
            return {}
        maximum = max(scores.values())
        if maximum <= 0:
            return {key: 0.0 for key in scores}
        return {key: value / maximum for key, value in scores.items()}
