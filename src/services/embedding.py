"""
Embedding service using sentence-transformers with multilingual-e5-base.
Supports Korean + English text for topic similarity matching.
Vectors stored as BLOB in MySQL (768 dims * 4 bytes = 3072 bytes per topic).
"""

import numpy as np
from typing import Optional

MODEL_NAME = "intfloat/multilingual-e5-base"

_model = None


def get_model():
    """Lazy-load the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_query(text: str) -> np.ndarray:
    """Encode a search query. Prefix 'query: ' is required by e5 models."""
    model = get_model()
    return model.encode(f"query: {text}", normalize_embeddings=True)


def embed_passage(text: str) -> np.ndarray:
    """Encode a stored topic text. Prefix 'passage: ' is required by e5 models."""
    model = get_model()
    return model.encode(f"passage: {text}", normalize_embeddings=True)


def serialize_embedding(vec: np.ndarray) -> bytes:
    """Convert numpy vector to bytes for MySQL BLOB storage."""
    return vec.astype(np.float32).tobytes()


def deserialize_embedding(blob: bytes) -> Optional[np.ndarray]:
    """Convert MySQL BLOB bytes back to numpy vector."""
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def compute_similarity(query_vec: np.ndarray, stored_vec: np.ndarray) -> float:
    """Cosine similarity between two L2-normalized vectors (dot product)."""
    return float(np.dot(query_vec, stored_vec))


def find_similar_topics(
    query_text: str,
    stored_topics: list,
    threshold: float = 0.75,
    top_k: int = 5,
) -> list:
    """
    Find topics similar to query_text from a list of stored topics.

    Args:
        query_text: The text to search for (title + summary combined)
        stored_topics: List of dicts with 'id', 'title', 'embedding' (bytes)
        threshold: Minimum cosine similarity score
        top_k: Maximum number of results

    Returns:
        List of dicts with 'id', 'title', 'score', sorted by score desc
    """
    query_vec = embed_query(query_text)

    results = []
    for topic in stored_topics:
        if topic.get("embedding") is None:
            continue
        stored_vec = deserialize_embedding(topic["embedding"])
        if stored_vec is None:
            continue
        similarity = compute_similarity(query_vec, stored_vec)
        if similarity >= threshold:
            results.append({
                "id": topic["id"],
                "title": topic["title"],
                "score": round(similarity, 4),
                "status": topic.get("status", "unknown"),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
