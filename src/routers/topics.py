import hashlib
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

try:
    from ..connectors import get_db_connection
    from ..models.topics import (
        BulkCreateTopicsRequest,
        CreateBlogPostRequest,
        CreateQuestionsRequest,
        CreateTopicRequest,
        CreateTopicSourceRequest,
        SaveInsightExchangesRequest,
        SubmitAnswersRequest,
        UpdateTopicStatusRequest,
    )
    from ..services.embedding import (
        embed_passage,
        find_similar_topics,
        serialize_embedding,
    )
except ImportError:
    from connectors import get_db_connection
    from models.topics import (
        BulkCreateTopicsRequest,
        CreateBlogPostRequest,
        CreateQuestionsRequest,
        CreateTopicRequest,
        CreateTopicSourceRequest,
        SaveInsightExchangesRequest,
        SubmitAnswersRequest,
        UpdateTopicStatusRequest,
    )
    from services.embedding import (
        embed_passage,
        find_similar_topics,
        serialize_embedding,
    )

router = APIRouter(prefix="/api/topics", tags=["topics"])
sources_router = APIRouter(prefix="/api/topic-sources", tags=["topic-sources"])


def _generate_id():
    return str(uuid.uuid4())


def _make_unique_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


# ============ Topic Sources ============


@sources_router.get("")
async def get_topic_sources():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM topic_sources ORDER BY created_at DESC")
            sources = cursor.fetchall()
            return [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "url": s["url"],
                    "type": s["type"],
                    "crawlMethod": s["crawl_method"],
                    "isActive": bool(s["is_active"]),
                    "crawlIntervalHours": s["crawl_interval_hours"],
                    "lastCrawledAt": s["last_crawled_at"].isoformat() if s["last_crawled_at"] else None,
                    "createdAt": s["created_at"].isoformat(),
                }
                for s in sources
            ]


@sources_router.post("", status_code=201)
async def create_topic_source(data: CreateTopicSourceRequest):
    source_id = _generate_id()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO topic_sources (id, name, url, type, crawl_method, crawl_interval_hours)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (source_id, data.name, data.url, data.type, data.crawl_method, data.crawl_interval_hours),
            )
    return {"id": source_id, "name": data.name, "url": data.url, "type": data.type}


@sources_router.patch("/{source_id}/crawled")
async def update_source_crawled(source_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE topic_sources SET last_crawled_at = %s WHERE id = %s",
                (datetime.now(), source_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Source not found")
    return {"message": "Updated"}


# ============ Topics CRUD ============


@router.get("")
async def get_topics(
    status: Optional[str] = Query(None),
    sort: str = Query("score"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            where_clauses = []
            params = []

            if status:
                where_clauses.append("t.status = %s")
                params.append(status)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            sort_map = {
                "score": "t.score DESC",
                "created_at": "t.created_at DESC",
                "controversy": "t.score_controversy DESC",
                "novelty": "t.score_novelty DESC",
            }
            order_sql = sort_map.get(sort, "t.score DESC")

            cursor.execute(
                f"""
                SELECT t.id, t.title, t.original_url, t.summary, t.score,
                       t.score_controversy, t.score_novelty, t.score_helpfulness,
                       t.status, t.previous_topic_id, t.created_at
                FROM topics t
                {where_sql}
                ORDER BY {order_sql}
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            topics = cursor.fetchall()

            topic_ids = [t["id"] for t in topics]
            hashtags_map = {}
            if topic_ids:
                placeholders = ",".join(["%s"] * len(topic_ids))
                cursor.execute(
                    f"SELECT topic_id, tag FROM topic_hashtags WHERE topic_id IN ({placeholders})",
                    topic_ids,
                )
                for row in cursor.fetchall():
                    hashtags_map.setdefault(row["topic_id"], []).append(row["tag"])

            return [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "originalUrl": t["original_url"],
                    "summary": t["summary"],
                    "score": t["score"],
                    "scoreControversy": t["score_controversy"],
                    "scoreNovelty": t["score_novelty"],
                    "scoreHelpfulness": t["score_helpfulness"],
                    "status": t["status"],
                    "hashtags": hashtags_map.get(t["id"], []),
                    "hasPrevious": t["previous_topic_id"] is not None,
                    "createdAt": t["created_at"].isoformat(),
                }
                for t in topics
            ]


@router.get("/{topic_id}")
async def get_topic(topic_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, source_id, title, original_url, source_page_url, summary,
                       score, score_controversy, score_novelty, score_helpfulness,
                       status, unique_hash, previous_topic_id, created_at, updated_at
                FROM topics WHERE id = %s
                """,
                (topic_id,),
            )
            topic = cursor.fetchone()
            if not topic:
                raise HTTPException(status_code=404, detail="Topic not found")

            cursor.execute(
                "SELECT tag FROM topic_hashtags WHERE topic_id = %s", (topic_id,),
            )
            hashtags = [r["tag"] for r in cursor.fetchall()]

            cursor.execute(
                "SELECT id, url, title, description FROM topic_references WHERE topic_id = %s",
                (topic_id,),
            )
            references = [
                {"id": r["id"], "url": r["url"], "title": r["title"], "description": r["description"]}
                for r in cursor.fetchall()
            ]

            cursor.execute(
                """
                SELECT q.id, q.question_text, q.question_type, q.display_order,
                       a.answer_text, a.answered_at
                FROM topic_questions q
                LEFT JOIN topic_answers a ON q.id = a.question_id
                WHERE q.topic_id = %s
                ORDER BY q.display_order
                """,
                (topic_id,),
            )
            questions = [
                {
                    "id": q["id"],
                    "questionText": q["question_text"],
                    "questionType": q["question_type"],
                    "displayOrder": q["display_order"],
                    "answer": q["answer_text"],
                    "answeredAt": q["answered_at"].isoformat() if q["answered_at"] else None,
                }
                for q in cursor.fetchall()
            ]

            return {
                "id": topic["id"],
                "sourceId": topic["source_id"],
                "title": topic["title"],
                "originalUrl": topic["original_url"],
                "sourcePageUrl": topic["source_page_url"],
                "summary": topic["summary"],
                "score": topic["score"],
                "scoreControversy": topic["score_controversy"],
                "scoreNovelty": topic["score_novelty"],
                "scoreHelpfulness": topic["score_helpfulness"],
                "status": topic["status"],
                "uniqueHash": topic["unique_hash"],
                "previousTopicId": topic["previous_topic_id"],
                "hashtags": hashtags,
                "references": references,
                "questions": questions,
                "createdAt": topic["created_at"].isoformat(),
                "updatedAt": topic["updated_at"].isoformat(),
            }


def _insert_topic(cursor, data: CreateTopicRequest) -> dict:
    """Insert a single topic with its related data."""
    unique_hash = _make_unique_hash(data.original_url)

    cursor.execute("SELECT id FROM topics WHERE unique_hash = %s", (unique_hash,))
    if cursor.fetchone():
        return {"skipped": True, "url": data.original_url, "reason": "duplicate"}

    topic_id = _generate_id()
    now = datetime.now()

    # Generate embedding from title + summary
    embedding_text = f"{data.title} {data.summary}"
    try:
        embedding_vec = embed_passage(embedding_text)
        embedding_blob = serialize_embedding(embedding_vec)
    except Exception:
        embedding_blob = None

    cursor.execute(
        """
        INSERT INTO topics (id, source_id, title, original_url, source_page_url, summary,
                           score, score_controversy, score_novelty, score_helpfulness,
                           status, unique_hash, embedding, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s, %s, %s, %s)
        """,
        (
            topic_id, data.source_id, data.title, data.original_url,
            data.source_page_url, data.summary, data.score,
            data.score_controversy, data.score_novelty, data.score_helpfulness,
            unique_hash, embedding_blob, now, now,
        ),
    )

    for tag in data.hashtags:
        cursor.execute(
            "INSERT INTO topic_hashtags (id, topic_id, tag) VALUES (%s, %s, %s)",
            (_generate_id(), topic_id, tag),
        )

    for ref in data.references:
        cursor.execute(
            "INSERT INTO topic_references (id, topic_id, url, title, description) VALUES (%s, %s, %s, %s, %s)",
            (_generate_id(), topic_id, ref.get("url", ""), ref.get("title"), ref.get("description")),
        )

    for i, q in enumerate(data.questions):
        cursor.execute(
            "INSERT INTO topic_questions (id, topic_id, question_text, question_type, display_order) VALUES (%s, %s, %s, %s, %s)",
            (_generate_id(), topic_id, q.get("question_text", ""), q.get("question_type", "open"), q.get("display_order", i)),
        )

    return {"id": topic_id, "title": data.title, "score": data.score, "skipped": False}


@router.post("", status_code=201)
async def create_topic(data: CreateTopicRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            result = _insert_topic(cursor, data)
            if result.get("skipped"):
                raise HTTPException(status_code=409, detail=f"Duplicate topic: {result['url']}")
            return result


@router.post("/bulk", status_code=201)
async def bulk_create_topics(data: BulkCreateTopicsRequest):
    results = {"created": [], "skipped": []}
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for topic_data in data.topics:
                result = _insert_topic(cursor, topic_data)
                if result.get("skipped"):
                    results["skipped"].append(result)
                else:
                    results["created"].append(result)
    return {
        "createdCount": len(results["created"]),
        "skippedCount": len(results["skipped"]),
        "created": results["created"],
        "skipped": results["skipped"],
    }


@router.patch("/{topic_id}/status")
async def update_topic_status(topic_id: str, data: UpdateTopicStatusRequest):
    valid_statuses = {"new", "selected", "answered", "completed"}
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE topics SET status = %s, updated_at = %s WHERE id = %s",
                (data.status, datetime.now(), topic_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Topic not found")
    return {"id": topic_id, "status": data.status}


# ============ Similar Topics (Embedding) ============


@router.get("/{topic_id}/similar")
async def get_similar_topics(topic_id: str, threshold: float = Query(0.75), top_k: int = Query(5)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT title, summary FROM topics WHERE id = %s", (topic_id,))
            topic = cursor.fetchone()
            if not topic:
                raise HTTPException(status_code=404, detail="Topic not found")

            cursor.execute(
                "SELECT id, title, embedding, status FROM topics WHERE embedding IS NOT NULL AND id != %s",
                (topic_id,),
            )
            stored = cursor.fetchall()

            query_text = f"{topic['title']} {topic['summary']}"
            results = find_similar_topics(query_text, stored, threshold=threshold, top_k=top_k)
            return results


@router.post("/{topic_id}/link-previous")
async def link_previous_topic(topic_id: str, previous_topic_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (previous_topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Previous topic not found")

            cursor.execute(
                "UPDATE topics SET previous_topic_id = %s, updated_at = %s WHERE id = %s",
                (previous_topic_id, datetime.now(), topic_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Topic not found")
    return {"id": topic_id, "previousTopicId": previous_topic_id}


# ============ Questions & Answers ============


@router.get("/{topic_id}/questions")
async def get_topic_questions(topic_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            cursor.execute(
                """
                SELECT q.id, q.topic_id, q.question_text, q.question_type, q.display_order,
                       a.answer_text, a.answered_at
                FROM topic_questions q
                LEFT JOIN topic_answers a ON q.id = a.question_id
                WHERE q.topic_id = %s
                ORDER BY q.display_order
                """,
                (topic_id,),
            )
            questions = cursor.fetchall()
            return [
                {
                    "id": q["id"],
                    "topicId": q["topic_id"],
                    "questionText": q["question_text"],
                    "questionType": q["question_type"],
                    "displayOrder": q["display_order"],
                    "answer": q["answer_text"],
                    "answeredAt": q["answered_at"].isoformat() if q["answered_at"] else None,
                }
                for q in questions
            ]


@router.post("/{topic_id}/questions", status_code=201)
async def create_topic_questions(topic_id: str, data: CreateQuestionsRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            created = []
            for i, q in enumerate(data.questions):
                q_id = _generate_id()
                cursor.execute(
                    """
                    INSERT INTO topic_questions (id, topic_id, question_text, question_type, display_order)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (q_id, topic_id, q.get("question_text", ""), q.get("question_type", "open"), q.get("display_order", i)),
                )
                created.append({"id": q_id, "questionText": q["question_text"]})
    return {"createdCount": len(created), "questions": created}


@router.post("/{topic_id}/answers", status_code=201)
async def submit_topic_answers(topic_id: str, data: SubmitAnswersRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            now = datetime.now()
            for ans in data.answers:
                answer_id = _generate_id()
                cursor.execute(
                    """
                    INSERT INTO topic_answers (id, question_id, topic_id, answer_text, answered_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE answer_text = VALUES(answer_text), answered_at = VALUES(answered_at)
                    """,
                    (answer_id, ans["question_id"], topic_id, ans["answer_text"], now),
                )

            cursor.execute(
                "UPDATE topics SET status = 'answered', updated_at = %s WHERE id = %s",
                (now, topic_id),
            )
    return {"message": "Answers saved", "count": len(data.answers)}


# ============ Insight Exchanges ============


@router.get("/{topic_id}/exchanges")
async def get_insight_exchanges(topic_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            cursor.execute(
                """
                SELECT id, topic_id, exchange_order, ai_question, ai_context,
                       user_answer, reference_urls, reference_data, created_at
                FROM topic_insight_exchanges
                WHERE topic_id = %s
                ORDER BY exchange_order
                """,
                (topic_id,),
            )
            exchanges = cursor.fetchall()
            import json as _json
            return [
                {
                    "id": e["id"],
                    "topicId": e["topic_id"],
                    "exchangeOrder": e["exchange_order"],
                    "aiQuestion": e["ai_question"],
                    "aiContext": e["ai_context"],
                    "userAnswer": e["user_answer"],
                    "referenceUrls": _json.loads(e["reference_urls"]) if e["reference_urls"] else [],
                    "referenceData": e["reference_data"],
                    "createdAt": e["created_at"].isoformat(),
                }
                for e in exchanges
            ]


@router.post("/{topic_id}/exchanges", status_code=201)
async def save_insight_exchanges(topic_id: str, data: SaveInsightExchangesRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            import json as _json
            created = []
            for i, ex in enumerate(data.exchanges):
                ex_id = _generate_id()
                ref_urls = ex.get("reference_urls", [])
                cursor.execute(
                    """
                    INSERT INTO topic_insight_exchanges
                        (id, topic_id, exchange_order, ai_question, ai_context,
                         user_answer, reference_urls, reference_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ex_id, topic_id, ex.get("exchange_order", i),
                        ex.get("ai_question", ""),
                        ex.get("ai_context"),
                        ex.get("user_answer", ""),
                        _json.dumps(ref_urls, ensure_ascii=False) if ref_urls else None,
                        ex.get("reference_data"),
                    ),
                )
                created.append({"id": ex_id, "exchangeOrder": ex.get("exchange_order", i)})

    return {"createdCount": len(created), "exchanges": created}


# ============ Blog Posts ============


@router.get("/{topic_id}/blog")
async def get_topic_blog(topic_id: str):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM topic_blog_posts WHERE topic_id = %s", (topic_id,))
            blog = cursor.fetchone()
            if not blog:
                return None

            return {
                "id": blog["id"],
                "topicId": blog["topic_id"],
                "title": blog["title"],
                "content": blog["content"],
                "version": blog["version"],
                "createdAt": blog["created_at"].isoformat(),
                "updatedAt": blog["updated_at"].isoformat(),
            }


@router.post("/{topic_id}/blog", status_code=201)
async def create_or_update_blog(topic_id: str, data: CreateBlogPostRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Topic not found")

            now = datetime.now()

            cursor.execute(
                "SELECT id, version FROM topic_blog_posts WHERE topic_id = %s", (topic_id,),
            )
            existing = cursor.fetchone()

            if existing:
                new_version = existing["version"] + 1
                cursor.execute(
                    """
                    UPDATE topic_blog_posts
                    SET title = %s, content = %s, version = %s, updated_at = %s
                    WHERE topic_id = %s
                    """,
                    (data.title, data.content, new_version, now, topic_id),
                )
                blog_id = existing["id"]
            else:
                blog_id = _generate_id()
                new_version = 1
                cursor.execute(
                    """
                    INSERT INTO topic_blog_posts (id, topic_id, title, content, version, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 1, %s, %s)
                    """,
                    (blog_id, topic_id, data.title, data.content, now, now),
                )

            cursor.execute(
                "UPDATE topics SET status = 'completed', updated_at = %s WHERE id = %s",
                (now, topic_id),
            )

    return {
        "id": blog_id,
        "topicId": topic_id,
        "title": data.title,
        "version": new_version,
    }
