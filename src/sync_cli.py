"""`todo-sync` CLI — `python -m src.sync_cli <command>`.

    doctor         신원·스키마·커서·대기 건수·이슈·트리거 리포트
    link-identity  로컬 owner id 를 원격 계정 id 로 재작성 (최초 1회)
    pause / resume 동기화 일시 정지 / 재개
    bootstrap      원격을 로컬 데이터로 전량 덮어쓰기 (파괴적 — 확인 필요)

부트스트랩은 양방향 정합이 아니라 **일방 적재**다. 원격 데이터는 폐기 대상이므로
로컬을 원격에 그대로 올리고, 정상 운영은 그 시점 **이후부터** 시작된다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path

try:
    from .config import SYNC_BACKUP_DIR, SYNC_CLIENT_ID, SYNC_ENABLED, SYNC_PEER_URL, sync_role
    from .connectors import DB_CONFIG, get_db_connection, init_db
    from .sync_schema import SCHEMA_VERSION, SYNC_TABLE_ORDER, declared_tables
    from .timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from .services.sync_auth import distinct_owner_ids
    from .services.sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from .services.sync_store import (
        get_local_identity,
        get_sync_state,
        max_change_seq,
        pending_change_count,
        set_paused,
        unresolved_issue_counts,
        update_sync_state,
    )
except ImportError:  # pragma: no cover
    from config import SYNC_BACKUP_DIR, SYNC_CLIENT_ID, SYNC_ENABLED, SYNC_PEER_URL, sync_role
    from connectors import DB_CONFIG, get_db_connection, init_db
    from sync_schema import SCHEMA_VERSION, SYNC_TABLE_ORDER, declared_tables
    from timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from services.sync_auth import distinct_owner_ids
    from services.sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from services.sync_store import (
        get_local_identity,
        get_sync_state,
        max_change_seq,
        pending_change_count,
        set_paused,
        unresolved_issue_counts,
        update_sync_state,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def _fetch_handshake() -> tuple[dict | None, str | None]:
    peer = get_sync_peer()
    if not peer.configured:
        return None, "SYNC_PEER_URL / SYNC_KEY_ID / SYNC_SECRET 가 설정되지 않았습니다"
    try:
        return peer.handshake(), None
    except SyncPeerUnreachable as exc:
        return None, f"원격에 닿지 못했습니다 (오프라인): {exc}"
    except SyncPeerError as exc:
        return None, f"원격이 요청을 거절했습니다: {exc}"


def _table_counts(cursor) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in SYNC_TABLE_ORDER:
        cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
        counts[table] = int(cursor.fetchone()["n"])
        cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}` WHERE updated_at_utc IS NULL")
        missing = int(cursor.fetchone()["n"])
        if missing:
            counts[f"{table} (updated_at_utc 누락)"] = missing
    return counts


def _trigger_count(cursor) -> int:
    cursor.execute(
        """
        SELECT COUNT(*) AS n
        FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME LIKE 'trg_%_change_log'
        """
    )
    return int(cursor.fetchone()["n"])


def _local_identity_row(cursor) -> dict | None:
    """`project_members` 에 들어 있는 로컬 신원 (auth DB 를 열지 않고 확인 가능)."""
    cursor.execute(
        """
        SELECT user_id, username, display_name, email
        FROM project_members
        WHERE deleted_at IS NULL
        ORDER BY invited_at ASC
        LIMIT 1
        """
    )
    return cursor.fetchone()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: Namespace) -> int:
    handshake, peer_error = _fetch_handshake()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            state = get_sync_state(cursor, SYNC_CLIENT_ID)
            max_seq = max_change_seq(cursor)
            pending = pending_change_count(cursor, int(state["last_pushed_seq"]))
            owner_ids = distinct_owner_ids(cursor)
            counts = _table_counts(cursor)
            triggers = _trigger_count(cursor)
            issues = unresolved_issue_counts(cursor)
            identity_cache = get_local_identity(cursor)
            local_member = _local_identity_row(cursor)

    report = {
        "role": sync_role(),
        "syncEnabled": SYNC_ENABLED,
        "peerUrl": SYNC_PEER_URL or None,
        "clientId": SYNC_CLIENT_ID,
        "database": DB_CONFIG["database"],
        "schemaVersion": SCHEMA_VERSION,
        "changeLogTriggers": f"{triggers}/{len(SYNC_TABLE_ORDER) * 3}",
        "cursors": {
            "lastPushedSeq": int(state["last_pushed_seq"]),
            "lastPulledSeq": int(state["last_pulled_seq"]),
            "localMaxSeq": max_seq,
            "pendingPush": pending,
        },
        "paused": bool(state["paused"]),
        "lastOkAt": iso_utc(state["last_ok_at"]),
        "lastError": state["last_error"],
        "rowCounts": counts,
        "localOwnerIds": owner_ids,
        "localIdentityFromData": (
            {
                "userId": local_member["user_id"],
                "username": local_member.get("username"),
                "displayName": local_member.get("display_name"),
                "email": local_member.get("email"),
            }
            if local_member
            else None
        ),
        "identityCache": (
            {
                "accountId": identity_cache["account_id"],
                "loginId": identity_cache.get("login_id"),
                "email": identity_cache.get("email"),
                "permission": identity_cache.get("permission"),
                "verifiedAt": iso_utc(identity_cache.get("verified_at_utc")),
            }
            if identity_cache
            else None
        ),
        "unresolvedIssues": issues,
    }

    if handshake is None:
        report["peer"] = {"reachable": False, "error": peer_error}
    else:
        server_time = parse_iso_utc(handshake.get("serverTimeUtc"))
        skew = (
            abs((utcnow_naive() - server_time).total_seconds())
            if server_time is not None
            else None
        )
        remote_account = handshake.get("accountId")
        report["peer"] = {
            "reachable": True,
            "schemaVersion": handshake.get("schemaVersion"),
            "schemaMatch": handshake.get("schemaVersion") == SCHEMA_VERSION,
            "accountId": remote_account,
            "permission": handshake.get("permission"),
            "ownerIds": handshake.get("ownerIds"),
            "maxSeq": handshake.get("maxSeq"),
            "identity": handshake.get("identity"),
            "clockSkewSeconds": round(skew, 3) if skew is not None else None,
        }
        report["identityMatch"] = all(owner == remote_account for owner in owner_ids)
        report["mismatchedOwnerIds"] = [
            owner for owner in owner_ids if owner != remote_account
        ]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(report)

    problems = []
    if triggers != len(SYNC_TABLE_ORDER) * 3:
        problems.append("change_log 트리거가 모두 존재하지 않습니다 — init_db 를 다시 실행하세요")
    if any("누락" in key for key in counts):
        problems.append("updated_at_utc 백필이 남았습니다 — scripts/backfill_updated_at_utc.py")
    if report.get("mismatchedOwnerIds"):
        problems.append("owner id 가 원격 계정과 다릅니다 — link-identity 가 필요합니다")
    if problems:
        print("\n조치 필요:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    return 0


def _print_report(report: dict, indent: int = 0) -> None:
    pad = "  " * indent
    for key, value in report.items():
        if isinstance(value, dict):
            print(f"{pad}{key}:")
            _print_report(value, indent + 1)
        elif isinstance(value, list):
            print(f"{pad}{key}: {value if value else '[]'}")
        else:
            print(f"{pad}{key}: {value}")


# ---------------------------------------------------------------------------
# link-identity
# ---------------------------------------------------------------------------


def cmd_link_identity(args: Namespace) -> int:
    """로컬 owner id 를 원격 계정 id 로 재작성한다 (최초 1회, 단일 트랜잭션).

    **email 일치를 게이트로 쓰지 않는다** — 로컬 계정 email 이 원격과 다를 수 있다.
    두 신원을 나란히 보여주고 사람이 판단하며, 자동 거부는 "distinct id 가 2개 이상"일 때만 한다.
    """
    mapping = _parse_map(args.map)
    handshake, peer_error = _fetch_handshake()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            owner_ids = distinct_owner_ids(cursor)
            local_member = _local_identity_row(cursor)
            null_counts = _null_owner_counts(cursor)

    if handshake is None and not mapping:
        print(f"원격 신원을 확인할 수 없습니다: {peer_error}", file=sys.stderr)
        print("원격에 닿지 못하면 --map <old>=<new> 로 직접 지정해야 합니다.", file=sys.stderr)
        return 2

    remote_account = (handshake or {}).get("accountId")
    remote_identity = (handshake or {}).get("identity") or {}

    if not mapping:
        if len(owner_ids) != 1 or any(null_counts.values()):
            print(
                "자동 해석을 포기합니다 — distinct owner id 가 "
                f"{len(owner_ids)}개이고 NULL 은 {null_counts} 입니다.\n"
                "--map <old>=<new> 를 지정하세요.",
                file=sys.stderr,
            )
            return 2
        mapping = {owner_ids[0]: remote_account}

    print("=== 신원 대조 ===")
    print("로컬 (project_members 실측):")
    if local_member:
        print(f"  userId      : {local_member['user_id']}")
        print(f"  username    : {local_member.get('username')}")
        print(f"  displayName : {local_member.get('display_name')}")
        print(f"  email       : {local_member.get('email')}")
    else:
        print("  (project_members 행이 없습니다)")
    print("원격 (handshake):")
    print(f"  accountId   : {remote_account}")
    print(f"  loginId     : {remote_identity.get('loginId')}")
    print(f"  displayName : {remote_identity.get('displayName')}")
    print(f"  email       : {remote_identity.get('email')}")
    print(f"  출처        : {remote_identity.get('source')}")
    print("\n=== 재작성 계획 ===")
    for old, new in mapping.items():
        print(f"  {old}  →  {new}")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            affected = _count_identity_rows(cursor, list(mapping))
    print(f"  대상 행: {affected} (projects.owner_id + memos.created_by + project_members.user_id)")

    if args.dry_run:
        print("\n[dry-run] 아무것도 변경하지 않았습니다.")
        return 0

    if not args.yes:
        answer = input("\n로컬 신원을 원격에 맞춰 재작성합니다. 계속할까요? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("취소했습니다.")
            return 1

    # 신원 재작성은 로컬을 원격에 맞추는 **복구** 작업이므로 change_log 에 남기지 않는다
    # (원격은 이미 올바른 id 를 가지고 있다).
    with get_db_connection(sync_applying=True) as conn:
        with conn.cursor() as cursor:
            _assert_no_member_collision(cursor, mapping)
            total = 0
            for old, new in mapping.items():
                cursor.execute(
                    "UPDATE projects SET owner_id = %s WHERE owner_id = %s", (new, old)
                )
                total += cursor.rowcount
                cursor.execute(
                    "UPDATE memos SET created_by = %s WHERE created_by = %s", (new, old)
                )
                total += cursor.rowcount
                cursor.execute(
                    "UPDATE project_members SET user_id = %s WHERE user_id = %s", (new, old)
                )
                total += cursor.rowcount

            # 오래된 표시 정보가 원격으로 올라가지 않게 원격 신원 값으로 갱신한다
            if remote_account:
                cursor.execute(
                    """
                    UPDATE project_members
                    SET username = COALESCE(%s, username),
                        display_name = COALESCE(%s, display_name),
                        email = COALESCE(%s, email)
                    WHERE user_id = %s
                    """,
                    (
                        remote_identity.get("loginId"),
                        remote_identity.get("displayName"),
                        remote_identity.get("email"),
                        remote_account,
                    ),
                )

    print(f"\n완료 — {total}행 재작성.")
    return 0


def _parse_map(entries: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in entries or []:
        if "=" not in entry:
            raise SystemExit(f"--map 형식이 잘못되었습니다: {entry} (old=new)")
        old, new = entry.split("=", 1)
        mapping[old.strip()] = new.strip()
    return mapping


def _null_owner_counts(cursor) -> dict[str, int]:
    counts = {}
    for table, column in (
        ("projects", "owner_id"),
        ("memos", "created_by"),
        ("project_members", "user_id"),
    ):
        cursor.execute(
            f"SELECT COUNT(*) AS n FROM `{table}` WHERE {column} IS NULL OR {column} = ''"
        )
        counts[f"{table}.{column}"] = int(cursor.fetchone()["n"])
    return counts


def _count_identity_rows(cursor, old_ids: list[str]) -> int:
    if not old_ids:
        return 0
    placeholders = ", ".join(["%s"] * len(old_ids))
    total = 0
    for table, column in (
        ("projects", "owner_id"),
        ("memos", "created_by"),
        ("project_members", "user_id"),
    ):
        cursor.execute(
            f"SELECT COUNT(*) AS n FROM `{table}` WHERE {column} IN ({placeholders})",
            old_ids,
        )
        total += int(cursor.fetchone()["n"])
    return total


def _assert_no_member_collision(cursor, mapping: dict[str, str]) -> None:
    """`UNIQUE (project_id, user_id)` 충돌을 미리 잡는다 (owner id 가 1개면 발생하지 않는다)."""
    for old, new in mapping.items():
        cursor.execute(
            """
            SELECT a.project_id
            FROM project_members a
            JOIN project_members b ON a.project_id = b.project_id AND b.user_id = %s
            WHERE a.user_id = %s
            LIMIT 1
            """,
            (new, old),
        )
        collision = cursor.fetchone()
        if collision:
            raise SystemExit(
                f"project_members 충돌: project {collision['project_id']} 에 이미 {new} 가 있습니다. "
                "해당 멤버를 먼저 정리하세요."
            )


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


def cmd_pause(args: Namespace) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            set_paused(cursor, SYNC_CLIENT_ID, True)
    print(f"동기화를 일시 정지했습니다 (peer={SYNC_CLIENT_ID}).")
    return 0


def cmd_resume(args: Namespace) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            set_paused(cursor, SYNC_CLIENT_ID, False)
            update_sync_state(cursor, SYNC_CLIENT_ID, last_error=None)
    print(f"동기화를 재개했습니다 (peer={SYNC_CLIENT_ID}).")
    return 0


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _load_migrate_module():
    path = REPO_ROOT / "scripts" / "migrate_legacy_todo.py"
    spec = importlib.util.spec_from_file_location("migrate_legacy_todo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dump(label: str, host: str, port: int, user: str, password: str, database: str) -> Path | None:
    backup_dir = Path(SYNC_BACKUP_DIR)
    if not backup_dir.is_absolute():
        backup_dir = (REPO_ROOT / backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"todo-{label}-{database}-{stamp}.sql"

    env = {**os.environ, "MYSQL_PWD": password}
    command = [
        "mysqldump",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        "--single-transaction",
        "--routines",
        "--triggers",
        database,
    ]
    try:
        with target.open("wb") as handle:
            subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, check=True, env=env)
    except FileNotFoundError:
        target.unlink(missing_ok=True)
        print("mysqldump 를 찾을 수 없습니다 — --skip-dump 를 지정하거나 설치하세요.", file=sys.stderr)
        return None
    except subprocess.CalledProcessError as exc:
        target.unlink(missing_ok=True)
        print(f"mysqldump 실패: {exc.stderr.decode('utf-8', 'replace')[:500]}", file=sys.stderr)
        return None
    print(f"  덤프 저장: {target}")
    return target


def _inspect_bootstrap_target(migrate, args: Namespace) -> dict:
    """대상 DB가 handshake를 제공한 노드의 DB인지 read-only fingerprint로 확인한다."""
    connection = None
    try:
        connection = migrate.connect(
            args.target_host,
            args.target_port,
            args.target_user,
            args.target_password,
            args.target_database,
        )
        with connection.cursor() as cursor:
            actual_columns = {
                table: migrate.table_columns(cursor, table)
                for table in declared_tables()
            }
            cursor.execute(
                """
                SELECT DISTINCT owner_id AS account_id
                FROM projects
                WHERE owner_id IS NOT NULL AND owner_id <> ''
                """
            )
            owner_ids = sorted(row["account_id"] for row in cursor.fetchall())
            cursor.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM change_log")
            max_seq = int(cursor.fetchone()["max_seq"])
            server_identity = _database_server_identity(cursor)
        return {
            "columns": actual_columns,
            "ownerIds": owner_ids,
            "maxSeq": max_seq,
            "serverIdentity": server_identity,
            "error": None,
        }
    except Exception as exc:
        return {
            "columns": {},
            "ownerIds": [],
            "maxSeq": None,
            "serverIdentity": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if connection is not None:
            connection.close()


def _database_server_identity(cursor) -> dict:
    """호스트 별칭과 무관한 DB 서버/스키마 식별자.

    MySQL은 ``@@server_uuid``를 제공하지만 MariaDB에는 없을 수 있다. 이때는 서버가
    직접 보고하는 hostname/port와 현재 database를 사용한다. bootstrap은 두 연결의
    결과를 비교하므로 ``localhost``/``127.0.0.1`` 같은 클라이언트 문자열 별칭에
    의존하지 않는다.
    """
    try:
        cursor.execute(
            """
            SELECT @@server_uuid AS server_uuid,
                   @@hostname AS server_host,
                   @@port AS server_port,
                   DATABASE() AS database_name
            """
        )
    except Exception:
        cursor.execute(
            """
            SELECT NULL AS server_uuid,
                   @@hostname AS server_host,
                   @@port AS server_port,
                   DATABASE() AS database_name
            """
        )
    row = cursor.fetchone()
    return {
        "serverUuid": row.get("server_uuid"),
        "serverHost": row.get("server_host"),
        "serverPort": int(row["server_port"]),
        "database": row.get("database_name"),
    }


def _bootstrap_blockers(
    *,
    handshake: dict | None,
    peer_error: str | None,
    owner_ids: list[str],
    identity_cache: dict | None,
    local_counts: dict[str, int],
    target_fingerprint: dict,
    local_server_identity: dict | None,
) -> list[str]:
    """파괴적 bootstrap 전에 반드시 만족해야 하는, 우회 불가능한 불변조건."""
    blockers: list[str] = []
    if handshake is None:
        blockers.append(f"인증된 원격 handshake가 필요합니다 ({peer_error})")
        remote_account = None
    else:
        remote_account = handshake.get("accountId")
        if handshake.get("schemaVersion") != SCHEMA_VERSION:
            blockers.append(
                "원격 schemaVersion이 정확히 일치하지 않습니다 "
                f"({handshake.get('schemaVersion')} != {SCHEMA_VERSION})"
            )
        if handshake.get("tables") != declared_tables():
            blockers.append("원격 동기화 테이블/컬럼 선언이 로컬과 정확히 일치하지 않습니다")
        if not isinstance(remote_account, str) or not remote_account.strip():
            blockers.append("handshake에 유효한 accountId가 없습니다")
            remote_account = None
        identity = handshake.get("identity")
        if not isinstance(identity, dict) or identity.get("accountId") != remote_account:
            blockers.append("handshake identity.accountId가 인증된 accountId와 일치하지 않습니다")
        if not handshake.get("permission") or not handshake.get("subjectKind"):
            blockers.append("handshake에 인증 주체의 permission/subjectKind가 없습니다")

    if identity_cache is None:
        blockers.append("신원 캐시가 없습니다 — 온라인일 때 원격 auth로 1회 로그인하세요")
    elif remote_account and identity_cache.get("account_id") != remote_account:
        blockers.append(
            "로컬 신원 캐시 account_id가 원격 계정과 다릅니다 "
            f"({identity_cache.get('account_id')} != {remote_account})"
        )

    if any("누락" in key for key in local_counts):
        blockers.append(
            "updated_at_utc 백필이 남았습니다 — scripts/backfill_updated_at_utc.py"
        )
    if remote_account:
        mismatched = [owner for owner in owner_ids if owner != remote_account]
        if mismatched:
            blockers.append(
                f"owner id가 원격 계정과 다릅니다 {mismatched} != {remote_account} "
                "— link-identity 먼저"
            )
    if (
        local_server_identity
        and target_fingerprint.get("serverIdentity") == local_server_identity
    ):
        blockers.append("대상 DB가 로컬 소스 DB와 같습니다 — 자기 자신을 wipe할 수 없습니다")

    target_error = target_fingerprint.get("error")
    if target_error:
        blockers.append(f"대상 DB fingerprint를 확인할 수 없습니다 ({target_error})")
        return blockers

    expected_tables = declared_tables()
    actual_columns = target_fingerprint.get("columns") or {}
    for table, expected_columns in expected_tables.items():
        actual = set(actual_columns.get(table) or [])
        missing = [column for column in expected_columns if column not in actual]
        if missing:
            blockers.append(f"대상 DB {table}에 동기화 컬럼이 없습니다: {missing}")

    if handshake is not None:
        handshake_owner_ids = sorted(handshake.get("ownerIds") or [])
        if target_fingerprint.get("ownerIds") != handshake_owner_ids:
            blockers.append(
                "대상 DB owner 목록이 handshake 응답과 다릅니다 "
                f"({target_fingerprint.get('ownerIds')} != {handshake_owner_ids})"
            )
        try:
            handshake_max_seq = int(handshake.get("maxSeq"))
        except (TypeError, ValueError):
            blockers.append("handshake에 유효한 maxSeq가 없습니다")
        else:
            if target_fingerprint.get("maxSeq") != handshake_max_seq:
                blockers.append(
                    "대상 DB change_log 상한이 handshake 응답과 다릅니다 "
                    f"({target_fingerprint.get('maxSeq')} != {handshake_max_seq})"
                )
    return blockers


def cmd_bootstrap(args: Namespace) -> int:
    """원격을 로컬 데이터로 전량 덮어쓴다.

    순서를 지켜야 한다: ① 원격 auth 1회 로그인(신원 캐시) → ② link-identity →
    ③ 적재. 순서가 바뀌면 원격에 존재하지 않는 계정 id 로 소유된 데이터가 굳는다.
    """
    migrate = _load_migrate_module()

    handshake, peer_error = _fetch_handshake()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            owner_ids = distinct_owner_ids(cursor)
            identity_cache = get_local_identity(cursor)
            local_counts = _table_counts(cursor)
            local_max_seq = max_change_seq(cursor)
            local_server_identity = _database_server_identity(cursor)
    target_fingerprint = _inspect_bootstrap_target(migrate, args)

    print("=== 부트스트랩 사전 점검 ===")
    print(f"  로컬 DB           : {DB_CONFIG['database']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    print(f"  원격 DB           : {args.target_database} @ {args.target_host}:{args.target_port}")
    print(f"  로컬 owner id     : {owner_ids}")
    print(f"  신원 캐시         : {'있음' if identity_cache else '없음'}")
    print(f"  로컬 행 수        : {local_counts}")
    print(f"  로컬 max(seq)     : {local_max_seq}")
    if handshake is None:
        print(f"  원격 handshake    : 실패 ({peer_error})")
    else:
        print(f"  원격 accountId    : {handshake.get('accountId')}")
        print(f"  원격 max(seq)     : {handshake.get('maxSeq')}")
        print(f"  원격 schema       : {handshake.get('schemaVersion')}")
    if target_fingerprint.get("error"):
        print(f"  대상 DB fingerprint: 실패 ({target_fingerprint['error']})")
    else:
        print(f"  대상 DB owner ids : {target_fingerprint['ownerIds']}")
        print(f"  대상 DB max(seq)  : {target_fingerprint['maxSeq']}")

    blockers = _bootstrap_blockers(
        handshake=handshake,
        peer_error=peer_error,
        owner_ids=owner_ids,
        identity_cache=identity_cache,
        local_counts=local_counts,
        target_fingerprint=target_fingerprint,
        local_server_identity=local_server_identity,
    )
    if blockers:
        print("\n중단 — 다음을 먼저 해결하세요:")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 2

    mirror_args = Namespace(
        mode="mirror",
        source_host=DB_CONFIG["host"],
        source_port=DB_CONFIG["port"],
        source_user=DB_CONFIG["user"],
        source_password=DB_CONFIG["password"],
        source_database=DB_CONFIG["database"],
        target_host=args.target_host,
        target_port=args.target_port,
        target_user=args.target_user,
        target_password=args.target_password,
        target_database=args.target_database,
        owner_user_id=None,
        owner_username=None,
        owner_display_name=None,
        owner_email=None,
        dry_run=args.dry_run,
        replace=True,
        confirm_replace=args.target_database,
        wipe_daily_tasks=True,
        sync_applying=True,
        allow_missing_utc=False,
    )

    if args.dry_run:
        print("\n=== [dry-run] wipe/적재 리허설 ===")
        migrate.migrate_mirror(mirror_args)
        print(
            "\n[dry-run] 원격은 변경되지 않았습니다. 실제 실행은 "
            f"--yes --confirm-replace {args.target_database} 가 필요합니다."
        )
        return 0

    if args.confirm_replace != args.target_database:
        print(
            f"\n파괴적 작업입니다. --confirm-replace '{args.target_database}' 를 지정하세요.",
            file=sys.stderr,
        )
        return 2
    if not args.yes:
        answer = input(
            f"\n원격 {args.target_database} 의 todo/daily-task 데이터를 모두 지우고 "
            "로컬 데이터로 채웁니다. 계속할까요? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("취소했습니다.")
            return 1

    if not args.skip_dump:
        print("\n=== 덤프 ===")
        local_dump = _dump(
            "local",
            DB_CONFIG["host"],
            DB_CONFIG["port"],
            DB_CONFIG["user"],
            DB_CONFIG["password"],
            DB_CONFIG["database"],
        )
        remote_dump = _dump(
            "remote",
            args.target_host,
            args.target_port,
            args.target_user,
            args.target_password,
            args.target_database,
        )
        if local_dump is None or remote_dump is None:
            print("덤프에 실패했습니다. --skip-dump 를 명시하지 않으면 진행하지 않습니다.", file=sys.stderr)
            return 2

    print("\n=== wipe + 적재 ===")
    migrate.migrate_mirror(mirror_args)

    print("\n=== 커서 기준선 ===")
    remote_max_seq = _remote_max_seq(migrate, mirror_args)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            local_max_seq = max_change_seq(cursor)
            update_sync_state(
                cursor,
                SYNC_CLIENT_ID,
                last_pushed_seq=local_max_seq,
                last_pulled_seq=remote_max_seq,
                last_ok_at=utcnow_naive(),
                last_error=None,
            )
    print(f"  last_pushed_seq = {local_max_seq} (로컬 max)")
    print(f"  last_pulled_seq = {remote_max_seq} (원격 max)")
    print("\n부트스트랩 완료. 이 시점 이후부터 원격이 진실의 원천입니다.")
    return 0


def _remote_max_seq(migrate, mirror_args: Namespace) -> int:
    connection = migrate.connect(
        mirror_args.target_host,
        mirror_args.target_port,
        mirror_args.target_user,
        mirror_args.target_password,
        mirror_args.target_database,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM change_log")
            return int(cursor.fetchone()["max_seq"])
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# init-db (편의)
# ---------------------------------------------------------------------------


def cmd_init_db(args: Namespace) -> int:
    _ = args
    init_db()
    return 0


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.sync_cli",
        description="todo 오프라인 동기화 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="신원·스키마·커서·이슈 리포트")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=cmd_doctor)

    link = sub.add_parser("link-identity", help="로컬 owner id 를 원격 계정 id 로 재작성")
    link.add_argument("--dry-run", action="store_true")
    link.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    link.add_argument(
        "--map",
        action="append",
        metavar="OLD=NEW",
        help="distinct owner id 가 2개 이상일 때 직접 지정 (반복 가능)",
    )
    link.set_defaults(handler=cmd_link_identity)

    pause = sub.add_parser("pause", help="동기화 일시 정지")
    pause.set_defaults(handler=cmd_pause)

    resume = sub.add_parser("resume", help="동기화 재개")
    resume.set_defaults(handler=cmd_resume)

    init = sub.add_parser("init-db", help="init_db() 실행 (스키마·트리거 반영)")
    init.set_defaults(handler=cmd_init_db)

    boot = sub.add_parser(
        "bootstrap",
        help="원격을 로컬 데이터로 전량 덮어쓰기 (파괴적)",
    )
    boot.add_argument("--target-host", required=True)
    boot.add_argument("--target-port", type=int, default=3306)
    boot.add_argument("--target-user", default="root")
    boot.add_argument("--target-password", default=os.getenv("SYNC_TARGET_DB_PASSWORD", ""))
    boot.add_argument("--target-database", required=True)
    boot.add_argument(
        "--dry-run",
        action="store_true",
        help="대상에는 SELECT만 실행해 wipe/적재 예정 건수를 리포트",
    )
    boot.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    boot.add_argument("--confirm-replace", default=None, metavar="TARGET_DB_NAME")
    boot.add_argument("--skip-dump", action="store_true", help="mysqldump 백업 생략 (권장하지 않음)")
    boot.set_defaults(handler=cmd_bootstrap)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
