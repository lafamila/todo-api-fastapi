from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import PublishArticleRequest
    from ..utils import can_manage_project, check_project_membership, generate_id, is_service_owner
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import PublishArticleRequest
    from utils import can_manage_project, check_project_membership, generate_id, is_service_owner


router = APIRouter(prefix="/api", tags=["articles"])


@router.post("/articles", status_code=201)
async def publish_article(
    data: PublishArticleRequest, user: dict = Depends(require_admin)
):
    """메모를 게시글로 발행 (현재 내용 스냅샷)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.project_id, m.title, m.content, m.created_at, p.owner_id,
                       COALESCE(MAX(mv.version), 0) + 1 as current_version
                FROM memos m
                JOIN projects p ON m.project_id = p.id
                LEFT JOIN memo_versions mv ON m.id = mv.memo_id
                WHERE m.id = %s AND m.deleted_at IS NULL
                GROUP BY m.id
            """,
                (data.memoId,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not can_manage_project(cursor, memo["project_id"], user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can publish article"
                )

            if not memo["content"]:
                raise HTTPException(status_code=400, detail="Cannot publish empty memo")

            cursor.execute("SELECT id FROM articles WHERE memo_id = %s", (data.memoId,))
            existing = cursor.fetchone()

            now = datetime.now()
            author_slug = user["slug"]

            if existing:
                cursor.execute(
                    """
                    UPDATE articles
                    SET title = %s, content = %s, published_version = %s,
                        author_id = %s, author_slug = %s,
                        published_at = %s, updated_at = %s
                    WHERE memo_id = %s
                """,
                    (
                        memo["title"],
                        memo["content"],
                        memo["current_version"],
                        user["id"],
                        author_slug,
                        now,
                        now,
                        data.memoId,
                    ),
                )
                article_id = existing["id"]
            else:
                article_id = generate_id()
                cursor.execute(
                    """
                    INSERT INTO articles
                        (id, memo_id, project_id, author_id, author_slug, title, content,
                         published_version, created_at, published_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        article_id,
                        data.memoId,
                        memo["project_id"],
                        user["id"],
                        author_slug,
                        memo["title"],
                        memo["content"],
                        memo["current_version"],
                        memo["created_at"],
                        now,
                        now,
                    ),
                )

            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.author_id, a.author_slug,
                       a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at,
                       p.name as project_name, p.icon as project_icon, p.is_secret
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "authorId": article["author_id"],
                "authorSlug": article["author_slug"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
                "projectName": article["project_name"],
                "projectIcon": article["project_icon"],
                "isSecret": bool(article["is_secret"]),
            }


@router.get("/articles")
async def get_articles(projectId: Optional[str] = None, authorSlug: Optional[str] = None):
    """게시글 목록 조회 (프로젝트/게시자 slug별 필터 가능)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            where = []
            params = []
            if projectId:
                where.append("a.project_id = %s")
                params.append(projectId)
            if authorSlug:
                where.append("a.author_slug = %s")
                params.append(authorSlug)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cursor.execute(
                f"""
                SELECT a.id, a.memo_id, a.project_id, a.author_id, a.author_slug, a.title,
                       a.published_version, a.created_at, a.published_at, a.updated_at,
                       p.name as project_name, p.icon as project_icon, p.is_secret
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                {where_sql}
                ORDER BY a.published_at DESC
            """,
                tuple(params),
            )
            articles = cursor.fetchall()

            return [
                {
                    "id": a["id"],
                    "memoId": a["memo_id"],
                    "projectId": a["project_id"],
                    "authorId": a["author_id"],
                    "authorSlug": a["author_slug"],
                    "title": a["title"],
                    "publishedVersion": a["published_version"],
                    "createdAt": a["created_at"].isoformat(),
                    "publishedAt": a["published_at"].isoformat(),
                    "updatedAt": a["updated_at"].isoformat(),
                    "projectName": a["project_name"],
                    "projectIcon": a["project_icon"],
                    "isSecret": bool(a["is_secret"]),
                }
                for a in articles
            ]


@router.get("/articles/boards/{author_slug}")
async def get_author_articles(author_slug: str):
    """특정 admin slug 게시판의 게시글 목록 조회"""
    return await get_articles(authorSlug=author_slug)


@router.get("/articles/{article_id}")
async def get_article(article_id: str):
    """게시글 상세 조회 (비밀 프로젝트 콘텐츠 제외)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.author_id, a.author_slug,
                       a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at,
                       p.name as project_name, p.icon as project_icon, p.is_secret
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()

            if not article:
                raise HTTPException(status_code=404, detail="Article not found")

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "authorId": article["author_id"],
                "authorSlug": article["author_slug"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
                "projectName": article["project_name"],
                "projectIcon": article["project_icon"],
                "isSecret": bool(article["is_secret"]),
            }


@router.delete("/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(require_admin)):
    """게시글 삭제 (게시 취소)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.id, a.project_id, a.author_id, p.owner_id
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()
            if not article:
                raise HTTPException(status_code=404, detail="Article not found")

            if article["author_id"] != user["id"] and not is_service_owner(user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can delete article"
                )

            cursor.execute("DELETE FROM articles WHERE id = %s", (article_id,))
            return {"message": "Article unpublished successfully"}


@router.get("/memos/{memo_id}/article")
async def get_memo_article(memo_id: str, user: dict = Depends(get_current_user)):
    """특정 메모의 게시 상태 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.author_id, a.author_slug,
                       a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at
                FROM articles a
                WHERE a.memo_id = %s
            """,
                (memo_id,),
            )
            article = cursor.fetchone()

            if not article:
                return None

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "authorId": article["author_id"],
                "authorSlug": article["author_slug"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
            }
