"""동기화 엔드포인트.

두 종류가 섞여 있다 — 인증 방식이 다르므로 혼동하지 말 것:

**피어 대상** (서버 역할만 서빙, auth 발급 service credential 인증)
    GET  /api/sync/handshake
    GET  /api/sync/changes?since=&limit=
    POST /api/sync/push
    POST /api/sync/locks/{memoId}/acquire | /release
    GET  /api/sync/locks/{memoId}
    POST /api/sync/merge/memos/{loserId}/merge-into/{winnerId}
    POST /api/sync/merge/projects/{loserId}/merge-into/{winnerId}

**UI 대상** (모든 역할이 서빙, 브라우저 세션 인증) — `todo-web-next` 가 쓴다
    GET  /api/sync/status
    GET  /api/sync/issues
    POST /api/sync/issues/resolve
    POST /api/sync/trigger

일반 CRUD 엔드포인트를 재사용하지 **않는다**: 타임스탬프가 다시 써지고, 메모 제목 중복
409 가드가 걸리고, 대량 처리가 안 된다. sync 경로는 그 가드를 우회하고 중복을
`sync_issues` 로 기록한다 (막으면 오프라인 생성분이 409 로 동기화를 영구 정지시킨다).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

try:
    from ..auth_utils import get_current_user, require_admin
    from ..config import (
        SYNC_BATCH_LIMIT,
        SYNC_CLIENT_ID,
        SYNC_ENABLED,
        SYNC_PEER_URL,
        runs_sync_daemon,
        serves_sync_peer_api,
        sync_role,
    )
    from ..connectors import get_db_connection
    from ..sync_schema import (
        SCHEMA_VERSION,
        SYNC_TABLES,
        declared_tables,
        filter_row,
        intersect_columns,
    )
    from ..timeutil import iso_utc, utcnow_naive
    from ..services.lock_registry import get_lock_registry
    from ..services.merge import MergeError, merge_memos, merge_projects
    from ..services.sync_apply import ReceiptPayloadMismatch, SIDE_CLIENT, apply_changes
    from ..services.sync_auth import (
        AccountResolutionError,
        SyncAuthError,
        SyncAuthUnavailable,
        SyncPrincipal,
        authenticate_headers,
        distinct_owner_ids,
        resolve_account_id,
    )
    from ..services.sync_runtime import get_sync_runtime
    from ..services.sync_store import (
        get_local_identity,
        get_sync_state,
        list_issues,
        max_change_seq,
        pending_change_count,
        pending_sync_retry_count,
        read_changes,
        resolve_issues,
        set_paused,
        visible_project_ids,
    )
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from config import (
        SYNC_BATCH_LIMIT,
        SYNC_CLIENT_ID,
        SYNC_ENABLED,
        SYNC_PEER_URL,
        runs_sync_daemon,
        serves_sync_peer_api,
        sync_role,
    )
    from connectors import get_db_connection
    from sync_schema import (
        SCHEMA_VERSION,
        SYNC_TABLES,
        declared_tables,
        filter_row,
        intersect_columns,
    )
    from timeutil import iso_utc, utcnow_naive
    from services.lock_registry import get_lock_registry
    from services.merge import MergeError, merge_memos, merge_projects
    from services.sync_apply import ReceiptPayloadMismatch, SIDE_CLIENT, apply_changes
    from services.sync_auth import (
        AccountResolutionError,
        SyncAuthError,
        SyncAuthUnavailable,
        SyncPrincipal,
        authenticate_headers,
        distinct_owner_ids,
        resolve_account_id,
    )
    from services.sync_runtime import get_sync_runtime
    from services.sync_store import (
        get_local_identity,
        get_sync_state,
        list_issues,
        max_change_seq,
        pending_change_count,
        pending_sync_retry_count,
        read_changes,
        resolve_issues,
        set_paused,
        visible_project_ids,
    )


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


# ---------------------------------------------------------------------------
# 요청 모델
# ---------------------------------------------------------------------------


class SyncChangeInput(BaseModel):
    table: str
    rowId: str = Field(min_length=1, max_length=50)
    op: str = "update"
    row: dict | None = None
    seq: int | None = Field(default=None, ge=1)
    baseUpdatedAtUtc: str | None = None
    basePeerSeq: int | None = Field(default=None, ge=0)


class SyncPushRequest(BaseModel):
    # daemon은 `<configured-client-id>:<persisted UUID epoch>`를 보낸다. epoch 없는
    # 임의 clientId를 허용하면 호출자가 receipt namespace를 매 요청마다 바꿔
    # 동일 source seq의 멱등성 검사를 우회할 수 있다.
    clientId: str = Field(
        min_length=38,
        max_length=128,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9._-]*:"
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
        ),
    )
    schemaVersion: int
    allowSchemaDrift: bool = False
    tables: dict[str, list[str]] | None = None
    changes: list[SyncChangeInput] = Field(
        min_length=1,
        max_length=SYNC_BATCH_LIMIT,
    )


class LockAcquireRequest(BaseModel):
    ownerKey: str
    userId: str
    displayName: str = ""


class LockReleaseRequest(BaseModel):
    ownerKey: str


class ResolveIssuesRequest(BaseModel):
    issueIds: list[str] | None = None
    kind: str | None = None
    refTable: str | None = None
    refId: str | None = None


class PauseRequest(BaseModel):
    paused: bool


# ---------------------------------------------------------------------------
# 피어 인증
# ---------------------------------------------------------------------------


async def require_sync_principal(request: Request) -> SyncPrincipal:
    """service credential 을 검증하고 `(accountId, permission)` 으로 정규화한다."""
    if not serves_sync_peer_api():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "sync_peer_api_not_served",
                "message": f"this node does not serve the sync peer API (role={sync_role()})",
            },
        )

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            try:
                account_id = resolve_account_id(cursor)
            except AccountResolutionError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "account_unresolved", "message": str(exc)},
                ) from exc

    try:
        return authenticate_headers(dict(request.headers), account_id)
    except SyncAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.reason, "message": exc.message},
        ) from exc
    except SyncAuthUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "auth_unavailable", "message": str(exc)},
        ) from exc


def _peer_columns(peer_tables: dict | None) -> dict[str, tuple[str, ...]]:
    """스키마 드리프트 경로 — 양쪽이 모두 아는 컬럼만 남긴다."""
    if not peer_tables:
        return {}
    resolved: dict[str, tuple[str, ...]] = {}
    for table in SYNC_TABLES:
        peer_columns = peer_tables.get(table)
        if peer_columns:
            resolved[table] = intersect_columns(table, list(peer_columns))
    return resolved


def _resolve_push_columns(body: SyncPushRequest) -> dict[str, tuple[str, ...]]:
    """push 스키마를 적용 **전에** 검증하고 명시적 drift 협상만 허용한다."""
    if body.schemaVersion == SCHEMA_VERSION:
        return {}
    if not body.allowSchemaDrift:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "sync_schema_mismatch",
                "serverSchemaVersion": SCHEMA_VERSION,
                "clientSchemaVersion": body.schemaVersion,
            },
        )
    if body.schemaVersion > SCHEMA_VERSION:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "sync_client_schema_ahead",
                "serverSchemaVersion": SCHEMA_VERSION,
                "clientSchemaVersion": body.schemaVersion,
            },
        )
    try:
        columns = _peer_columns(body.tables)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "sync_schema_negotiation_failed", "message": str(exc)},
        ) from exc
    missing_tables = sorted(set(SYNC_TABLES) - set(columns))
    if missing_tables:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "sync_schema_negotiation_failed",
                "missingTables": missing_tables,
            },
        )
    return columns


def _project_role(cursor, project_id: str, account_id: str) -> str | None:
    """프로젝트 소유권 또는 살아있는 멤버십 역할을 반환한다."""
    cursor.execute(
        """
        SELECT p.owner_id, pm.role
        FROM projects p
        LEFT JOIN project_members pm
               ON pm.project_id = p.id
              AND pm.user_id = %s
              AND pm.deleted_at IS NULL
        WHERE p.id = %s
        LIMIT 1
        """,
        (account_id, project_id),
    )
    project = cursor.fetchone()
    if project is None:
        return None
    if project.get("owner_id") == account_id:
        return "owner"
    return project.get("role")


def _existing_project_id(cursor, table: str, row_id: str) -> str | None:
    if table == "projects":
        cursor.execute("SELECT id FROM projects WHERE id = %s", (row_id,))
    elif table in ("memos", "project_members"):
        cursor.execute(f"SELECT project_id FROM `{table}` WHERE id = %s", (row_id,))
    elif table == "memo_versions":
        cursor.execute(
            """
            SELECT m.project_id
            FROM memo_versions mv
            JOIN memos m ON m.id = mv.memo_id
            WHERE mv.id = %s
            """,
            (row_id,),
        )
    else:
        return None
    row = cursor.fetchone()
    if row is None:
        return None
    return row.get("project_id") or row.get("id")


def _memo_project_id(
    cursor, memo_id: str | None, incoming_memos: dict[str, dict]
) -> str | None:
    if not memo_id:
        return None
    incoming = incoming_memos.get(memo_id)
    if incoming and incoming.get("project_id"):
        return str(incoming["project_id"])
    cursor.execute("SELECT project_id FROM memos WHERE id = %s", (memo_id,))
    memo = cursor.fetchone()
    return memo.get("project_id") if memo else None


def _forbidden_change(table: str, row_id: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": "sync_change_forbidden",
            "table": table,
            "rowId": row_id,
        },
    )


def _authorize_sync_changes(
    cursor,
    payload: list[dict],
    account_id: str,
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """batch 모든 행을 계정/프로젝트 역할로 검증한다.

    적용 전에 한 번에 수행하므로 한 행이라도 범위를 벗어나면 batch 전체가 롤백된다.
    `baseUpdatedAtUtc` 같은 프로토콜 메타데이터는 payload 에 보존하고, 권한 판정에 쓰는
    row 필드만 협상된 화이트리스트로 정규화한다.
    """
    normalized: list[tuple[dict, dict]] = []
    for change in payload:
        table = change.get("table")
        if table not in SYNC_TABLES:
            continue
        allowed = (columns_by_table or {}).get(table)
        row = filter_row(table, change.get("row") or {}, allowed)
        normalized.append((change, row))

    incoming_projects = {
        str(change["rowId"]): row
        for change, row in normalized
        if change.get("table") == "projects" and row
    }
    incoming_memos = {
        str(change["rowId"]): row
        for change, row in normalized
        if change.get("table") == "memos" and row
    }
    role_cache: dict[str, str | None] = {}

    def role_for(project_id: str | None) -> str | None:
        if not project_id:
            return None
        if project_id in role_cache:
            return role_cache[project_id]
        incoming = incoming_projects.get(project_id)
        cursor.execute("SELECT owner_id FROM projects WHERE id = %s", (project_id,))
        existing = cursor.fetchone()
        if existing is None and incoming and incoming.get("owner_id") == account_id:
            role = "owner"
        else:
            role = _project_role(cursor, project_id, account_id)
        role_cache[project_id] = role
        return role

    for change, row in normalized:
        table = str(change["table"])
        row_id = str(change.get("rowId") or "")
        if not row_id:
            raise _forbidden_change(table, row_id)
        existing_project = _existing_project_id(cursor, table, row_id)

        if table == "projects":
            cursor.execute("SELECT owner_id FROM projects WHERE id = %s", (row_id,))
            existing = cursor.fetchone()
            incoming_owner = row.get("owner_id")
            if existing is None:
                if not row or incoming_owner != account_id:
                    raise _forbidden_change(table, row_id)
                role_cache[row_id] = "owner"
                continue
            if _project_role(cursor, row_id, account_id) != "owner":
                raise _forbidden_change(table, row_id)
            if incoming_owner is not None and incoming_owner != existing.get("owner_id"):
                raise _forbidden_change(table, row_id)
            continue

        if table == "memos":
            incoming_project = row.get("project_id") or existing_project
            for project_id in {existing_project, incoming_project} - {None}:
                if role_for(str(project_id)) not in {"owner", "editor"}:
                    raise _forbidden_change(table, row_id)
            if not incoming_project:
                raise _forbidden_change(table, row_id)
            continue

        if table == "memo_versions":
            incoming_project = _memo_project_id(
                cursor, row.get("memo_id"), incoming_memos
            )
            for project_id in {existing_project, incoming_project} - {None}:
                if role_for(str(project_id)) not in {"owner", "editor"}:
                    raise _forbidden_change(table, row_id)
            if not (incoming_project or existing_project):
                raise _forbidden_change(table, row_id)
            continue

        if table == "project_members":
            incoming_project = row.get("project_id") or existing_project
            for project_id in {existing_project, incoming_project} - {None}:
                if role_for(str(project_id)) != "owner":
                    raise _forbidden_change(table, row_id)
            if not incoming_project:
                raise _forbidden_change(table, row_id)


def _require_memo_write_access(cursor, memo_id: str, account_id: str) -> None:
    cursor.execute("SELECT project_id FROM memos WHERE id = %s", (memo_id,))
    memo = cursor.fetchone()
    if memo is None:
        raise HTTPException(status_code=404, detail="Memo not found")
    if _project_role(cursor, memo["project_id"], account_id) not in {"owner", "editor"}:
        raise HTTPException(status_code=403, detail="Access denied")


def _handshake_identity(cursor, account_id: str) -> dict:
    """`link-identity` 가 사람에게 보여줄 원격 신원."""
    identity = get_local_identity(cursor, account_id)
    if identity is not None:
        return {
            "accountId": identity["account_id"],
            "loginId": identity.get("login_id"),
            "displayName": identity.get("display_name"),
            "email": identity.get("email"),
            "permission": identity.get("permission"),
            "source": "local_identity",
        }

    cursor.execute(
        """
        SELECT user_id, username, display_name, email
        FROM project_members
        WHERE user_id = %s AND deleted_at IS NULL
        ORDER BY invited_at ASC
        LIMIT 1
        """,
        (account_id,),
    )
    member = cursor.fetchone()
    if member is not None:
        return {
            "accountId": member["user_id"],
            "loginId": member.get("username"),
            "displayName": member.get("display_name"),
            "email": member.get("email"),
            "permission": None,
            "source": "project_members",
        }

    return {
        "accountId": account_id,
        "loginId": None,
        "displayName": None,
        "email": None,
        "permission": None,
        "source": "config",
    }


# ---------------------------------------------------------------------------
# 피어 엔드포인트
# ---------------------------------------------------------------------------


@router.get("/handshake")
async def sync_handshake(principal: SyncPrincipal = Depends(require_sync_principal)):
    """스키마 버전·계정·서버 시각·owner id 목록·커서 상한을 한 번에 준다.

    클라이언트는 이 응답으로 ① 스키마 드리프트 ② 신원 불일치 ③ 시계 편차 세 가지를
    동기화 **이전에** 판정한다.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "accountId": principal.account_id,
                "permission": principal.permission,
                "subjectKind": principal.subject_kind,
                "serverTimeUtc": iso_utc(utcnow_naive()),
                "ownerIds": distinct_owner_ids(cursor),
                "maxSeq": max_change_seq(cursor),
                "tables": declared_tables(),
                "identity": _handshake_identity(cursor, principal.account_id),
                "role": sync_role(),
            }


@router.get("/changes")
async def sync_changes(
    since: int = Query(0, ge=0),
    limit: int = Query(default=SYNC_BATCH_LIMIT, ge=1, le=2000),
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    """`since` 이후 변경을 **그 계정이 볼 수 있는 행만** 반환한다."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            changes, next_seq = read_changes(cursor, since, limit, principal.account_id)
            return {
                "schemaVersion": SCHEMA_VERSION,
                "changes": changes,
                "nextSeq": next_seq,
                "maxSeq": max_change_seq(cursor),
                "serverTimeUtc": iso_utc(utcnow_naive()),
            }


@router.post("/push")
async def sync_push(
    body: SyncPushRequest,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    """클라이언트 변경을 적용한다. 적용 커넥션은 `@sync_applying = 1` 로 두어 핑퐁을 막는다."""
    columns_by_table = _resolve_push_columns(body)
    dropped: dict[str, list[str]] = {}
    for change in body.changes:
        if change.table not in SYNC_TABLES or not change.row:
            continue
        allowed = set(
            columns_by_table.get(change.table)
            or SYNC_TABLES[change.table]["columns"]
        )
        unknown = sorted(set(change.row) - allowed)
        if unknown:
            dropped.setdefault(change.table, [])
            dropped[change.table] = sorted(set(dropped[change.table]) | set(unknown))

    payload = [change.model_dump() for change in body.changes]

    # 적용 자체는 sync_applying 커넥션에서 (충돌 보존 버전만 예외적으로 로그에 남는다)
    try:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(
                    cursor,
                    payload,
                    principal.account_id,
                    columns_by_table or None,
                )
                outcome = apply_changes(
                    cursor,
                    payload,
                    incoming_side=SIDE_CLIENT,
                    columns_by_table=columns_by_table or None,
                    receipt_account_id=principal.account_id,
                    receipt_client_id=body.clientId,
                )
    except ReceiptPayloadMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "receipt_payload_mismatch", "message": str(exc)},
        ) from exc

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            max_seq = max_change_seq(cursor)

    await _notify_sync_changed(principal.account_id, max_seq)

    response = outcome.as_dict()
    response.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "clientSchemaVersion": body.schemaVersion,
            "schemaMatch": body.schemaVersion == SCHEMA_VERSION,
            "droppedColumns": dropped,
            "maxSeq": max_seq,
            "nextSeq": max_seq,
            "serverTimeUtc": iso_utc(utcnow_naive()),
        }
    )
    return response


@router.post("/locks/{memo_id}/acquire")
async def sync_lock_acquire(
    memo_id: str,
    body: LockAcquireRequest,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    """위임된 락 획득. 서버가 단일 진실이므로 노드를 넘어 단일 작성자가 보장된다."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_memo_write_access(cursor, memo_id, principal.account_id)
    acquired, holder = get_lock_registry().acquire(
        memo_id, f"peer:{body.ownerKey}", body.userId, body.displayName or body.userId
    )
    return {"memoId": memo_id, "acquired": acquired, "holder": holder}


@router.post("/locks/{memo_id}/release")
async def sync_lock_release(
    memo_id: str,
    body: LockReleaseRequest,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_memo_write_access(cursor, memo_id, principal.account_id)
    released = get_lock_registry().release(memo_id, f"peer:{body.ownerKey}")
    return {"memoId": memo_id, "released": released is not None, "holder": None}


@router.get("/locks/{memo_id}")
async def sync_lock_holder(
    memo_id: str,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_memo_write_access(cursor, memo_id, principal.account_id)
    return {"memoId": memo_id, "holder": get_lock_registry().holder(memo_id)}


@router.post("/merge/memos/{loser_id}/merge-into/{winner_id}")
async def sync_merge_memo(
    loser_id: str,
    winner_id: str,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    """클라이언트가 위임한 메모 병합을 **서버에서** 실행한다.

    브라우저용 `/api/memos/{id}/merge-into/{id}` 와 같은 로직이지만 인증이 다르다
    (세션 쿠키가 아니라 service credential). 병합은 한쪽에서만 실행되어야 하므로
    클라이언트는 항상 이 경로로 위임하고 결과를 pull 로 받는다.
    """
    return await asyncio.to_thread(
        _execute_peer_merge, "memo", loser_id, winner_id, principal.account_id
    )


@router.post("/merge/projects/{loser_id}/merge-into/{winner_id}")
async def sync_merge_project(
    loser_id: str,
    winner_id: str,
    principal: SyncPrincipal = Depends(require_sync_principal),
):
    """클라이언트가 위임한 프로젝트 병합을 서버에서 실행한다."""
    return await asyncio.to_thread(
        _execute_peer_merge, "project", loser_id, winner_id, principal.account_id
    )


def _execute_peer_merge(kind: str, loser_id: str, winner_id: str, account_id: str) -> dict:
    # 병합 쓰기는 change_log 에 남아야 한다 (클라이언트가 pull 로 받는다) → 일반 커넥션
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for row_id in (loser_id, winner_id):
                project_id = _owning_project_id(cursor, kind, row_id)
                if project_id is None:
                    raise HTTPException(status_code=404, detail=f"Not found: {row_id}")
                required_roles = {"owner", "editor"} if kind == "memo" else {"owner"}
                if _project_role(cursor, project_id, account_id) not in required_roles:
                    raise HTTPException(status_code=403, detail="Access denied")
            try:
                if kind == "memo":
                    return merge_memos(cursor, loser_id, winner_id)
                return merge_projects(cursor, loser_id, winner_id)
            except MergeError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _owning_project_id(cursor, kind: str, row_id: str) -> str | None:
    if kind == "project":
        cursor.execute(
            "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL", (row_id,)
        )
        row = cursor.fetchone()
        return row["id"] if row else None
    cursor.execute(
        "SELECT project_id FROM memos WHERE id = %s AND deleted_at IS NULL", (row_id,)
    )
    row = cursor.fetchone()
    return row["project_id"] if row else None


_ADMIN_PERMISSIONS = {"superadmin", "owner", "admin"}


def _ensure_sync_admin(user: dict) -> None:
    if user.get("permission") not in _ADMIN_PERMISSIONS:
        raise HTTPException(status_code=403, detail="Admin access required")


def _issue_project_ids(cursor, issue: dict) -> set[str]:
    """이슈가 가리키는 실제 프로젝트 집합. dangling ref 는 빈 집합으로 숨긴다."""
    table = issue.get("refTable") or issue.get("ref_table")
    refs = {
        str(ref)
        for ref in (
            issue.get("refId") or issue.get("ref_id"),
            issue.get("peerRefId") or issue.get("peer_ref_id"),
        )
        if ref
    }
    if not refs or table not in SYNC_TABLES:
        return set()
    placeholders = ", ".join(["%s"] * len(refs))
    params = tuple(refs)
    if table == "projects":
        cursor.execute(
            f"SELECT id AS project_id FROM projects WHERE id IN ({placeholders})",
            params,
        )
    elif table in ("memos", "project_members"):
        cursor.execute(
            f"SELECT DISTINCT project_id FROM `{table}` WHERE id IN ({placeholders})",
            params,
        )
    else:
        cursor.execute(
            f"""
            SELECT DISTINCT m.project_id
            FROM memo_versions mv
            JOIN memos m ON m.id = mv.memo_id
            WHERE mv.id IN ({placeholders})
            """,
            params,
        )
    return {row["project_id"] for row in cursor.fetchall()}


def _issue_visible_to_user(
    cursor, issue: dict, user: dict, visible: set[str] | None = None
) -> bool:
    project_ids = _issue_project_ids(cursor, issue)
    if not project_ids:
        # schema/identity/clock 같은 노드 전역 이슈는 서비스 owner만 본다.
        return user.get("permission") in {"owner", "superadmin"}
    allowed = visible if visible is not None else visible_project_ids(cursor, user["id"])
    return project_ids.issubset(allowed)


def _scoped_issues(
    cursor,
    user: dict,
    *,
    kind: str | None = None,
    ref_table: str | None = None,
    ref_id: str | None = None,
    include_resolved: bool = False,
    limit: int = 200,
) -> list[dict]:
    visible = visible_project_ids(cursor, user["id"])
    # 필터 후 limit을 적용해야 앞쪽의 타 계정 이슈 때문에 본인 이슈가 밀리지 않는다.
    candidates = list_issues(
        cursor,
        kind=kind,
        ref_table=ref_table,
        ref_id=ref_id,
        include_resolved=include_resolved,
        limit=1000,
    )
    return [
        issue
        for issue in candidates
        if _issue_visible_to_user(cursor, issue, user, visible)
    ][:limit]


def _scoped_issue_counts(cursor, user: dict) -> dict[str, int]:
    visible = visible_project_ids(cursor, user["id"])
    cursor.execute(
        """
        SELECT id, kind, ref_table, ref_id, peer_ref_id
        FROM sync_issues
        WHERE resolved_at IS NULL
        """
    )
    counts: dict[str, int] = {}
    for issue in cursor.fetchall():
        if not _issue_visible_to_user(cursor, issue, user, visible):
            continue
        kind = str(issue["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _authorized_issue_ids(
    cursor, body: ResolveIssuesRequest, user: dict
) -> list[str]:
    conditions = ["resolved_at IS NULL"]
    params: list[object] = []
    if body.issueIds:
        conditions.append(f"id IN ({', '.join(['%s'] * len(body.issueIds))})")
        params.extend(body.issueIds)
    if body.kind:
        conditions.append("kind = %s")
        params.append(body.kind)
    if body.refTable:
        conditions.append("ref_table = %s")
        params.append(body.refTable)
    if body.refId:
        conditions.append("(ref_id = %s OR peer_ref_id = %s)")
        params.extend([body.refId, body.refId])
    cursor.execute(
        "SELECT id, kind, ref_table, ref_id, peer_ref_id "
        f"FROM sync_issues WHERE {' AND '.join(conditions)}",
        params,
    )
    issues = cursor.fetchall()
    visible = visible_project_ids(cursor, user["id"])
    return [
        issue["id"]
        for issue in issues
        if _issue_visible_to_user(cursor, issue, user, visible)
    ]


# ---------------------------------------------------------------------------
# UI 엔드포인트 (브라우저 세션)
# ---------------------------------------------------------------------------


@router.get("/status")
async def sync_status(user: dict = Depends(get_current_user)):  # noqa: B008
    """헤더의 작은 동기화 표시가 쓰는 응답. 항목별 해소는 `/issues` 가 담당한다."""
    runtime = get_sync_runtime().snapshot()
    role = sync_role()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            state = get_sync_state(cursor, SYNC_CLIENT_ID)
            max_seq = max_change_seq(cursor)
            pending = (
                pending_change_count(cursor, int(state["last_pushed_seq"]))
                + pending_sync_retry_count(cursor, SYNC_CLIENT_ID)
                if role == "client"
                else 0
            )
            issues = _scoped_issue_counts(cursor, user)

    paused = bool(state["paused"])
    return {
        "enabled": SYNC_ENABLED,
        "role": role,
        "paused": paused,
        "peer": SYNC_CLIENT_ID,
        "peerUrl": SYNC_PEER_URL or None,
        "schemaVersion": SCHEMA_VERSION,
        "lastPushedSeq": int(state["last_pushed_seq"]),
        "lastPulledSeq": int(state["last_pulled_seq"]),
        "maxSeq": max_seq,
        "pending": pending,
        "lastOkAt": iso_utc(state["last_ok_at"]),
        "lastError": state["last_error"] or runtime["lastError"],
        "issues": issues,
        "issueTotal": sum(issues.values()),
        # 오프라인에서는 병합 조작을 잠근다 (양쪽에서 각자 병합하면 결과가 달라진다)
        "mergeLocked": role == "client" and not runtime["online"],
        **runtime,
    }


@router.get("/issues")
async def sync_issues(
    kind: str | None = Query(default=None),
    refTable: str | None = Query(default=None),
    refId: str | None = Query(default=None),
    includeResolved: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict = Depends(require_admin),  # noqa: B008
):
    """미해결 우선 이슈 목록. 흐린 제목 표시와 좌우 해소 화면의 근거다."""
    _ensure_sync_admin(user)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            issues = _scoped_issues(
                cursor,
                user,
                kind=kind,
                ref_table=refTable,
                ref_id=refId,
                include_resolved=includeResolved,
                limit=limit,
            )
            return {
                "issues": issues,
                "counts": _scoped_issue_counts(cursor, user),
            }


@router.post("/issues/resolve")
async def sync_resolve_issues(
    body: ResolveIssuesRequest,
    user: dict = Depends(require_admin),  # noqa: B008
):
    """해소 표시 — 이 호출로 목록의 흐린 표시가 사라진다."""
    _ensure_sync_admin(user)
    if not (body.issueIds or body.kind or body.refTable or body.refId):
        raise HTTPException(
            status_code=400,
            detail="issueIds / kind / refTable / refId 중 하나는 있어야 합니다.",
        )
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            issue_ids = _authorized_issue_ids(cursor, body, user)
            resolved = (
                resolve_issues(cursor, issue_ids=issue_ids) if issue_ids else 0
            )
            return {
                "resolved": resolved,
                "counts": _scoped_issue_counts(cursor, user),
            }


@router.post("/pause")
async def sync_pause(
    body: PauseRequest = Body(...),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    """스키마 실험 중 실사용 동기화를 잠시 멈추는 안전장치."""
    _ensure_sync_admin(user)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            set_paused(cursor, SYNC_CLIENT_ID, body.paused)
    return {"paused": body.paused, "peer": SYNC_CLIENT_ID}


@router.post("/trigger")
async def sync_trigger(user: dict = Depends(require_admin)):  # noqa: B008
    """데몬을 즉시 한 바퀴 돌린다 (수동 "지금 동기화")."""
    _ensure_sync_admin(user)
    if not runs_sync_daemon():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_sync_client",
                "message": f"this node does not run the sync daemon (role={sync_role()})",
            },
        )
    get_sync_runtime().request_cycle()
    return {"requested": True}


async def _notify_sync_changed(account_id: str, max_seq: int) -> None:
    """전역 룸 `sync:<accountId>` 에 변경 알림. 상대 노드 데몬이 즉시 pull 한다."""
    try:
        from ..services.realtime import get_realtime_server
    except ImportError:  # pragma: no cover
        from services.realtime import get_realtime_server

    server = get_realtime_server()
    if server is not None:
        await server.emit_sync_changed(account_id, max_seq)
