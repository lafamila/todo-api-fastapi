#!/usr/bin/env python3
"""Read-only validation of a local replica project; never initializes a database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pymysql
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.services.memo_revision import memo_revision
from src.services.task_rollup import RollupValidationError, build_rollup


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", default="Novawork")
    parser.add_argument("--project", default="Works")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--snapshot", action="store_true", help="write an exact private test snapshot under /private/tmp")
    args = parser.parse_args()
    if not 1 <= args.limit <= 30:
        parser.error("read-only validation is limited to at most 30 recent memos")

    values = dotenv_values(REPO / ".env.local")
    connection = pymysql.connect(
        host="127.0.0.1", port=33306, database="teddynote", user=values.get("DB_USER") or "root",
        password=values.get("DB_PASSWORD") or "", charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cursor.execute("SELECT DATABASE() AS db, @@hostname AS hostname, @@tx_read_only AS read_only")
            target = cursor.fetchone()
            if target["db"] != "teddynote" or not target["read_only"]:
                raise RuntimeError("refusing an unverified read-only local target")
            if target["hostname"].split(".")[0].lower() != socket.gethostname().split(".")[0].lower():
                raise RuntimeError("database is not the native local replica host")
            cursor.execute(
                "SELECT child.id, child.name, parent.id AS parent_id, parent.name AS parent_name "
                "FROM projects child JOIN projects parent ON parent.id=child.parent_id "
                "WHERE child.name=%s AND parent.name=%s AND child.deleted_at IS NULL AND parent.deleted_at IS NULL",
                (args.project, args.parent),
            )
            projects = cursor.fetchall()
            if len(projects) != 1:
                raise RuntimeError("project path must identify exactly one live subproject")
            project = projects[0]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM memos WHERE project_id=%s AND kind='memo' AND deleted_at IS NULL",
                (project["id"],),
            )
            total = cursor.fetchone()["n"]
            cursor.execute(
                "SELECT id,title,content,created_at,updated_at,updated_at_utc FROM memos "
                "WHERE project_id=%s AND kind='memo' AND deleted_at IS NULL "
                "ORDER BY created_at DESC,id DESC LIMIT %s",
                (project["id"], args.limit),
            )
            memos = cursor.fetchall()
    finally:
        connection.rollback()
        connection.close()

    snapshots = [{**memo, "revision": memo_revision(memo)} for memo in memos]
    hashes = {memo["id"]: hashlib.sha256((memo["content"] or "").encode("utf-8")).hexdigest() for memo in memos}
    report = {
        "source": {"database": target["db"], "host": target["hostname"], "readOnly": True},
        "project": project, "order": "created_at DESC, id DESC", "totalMemoCount": total,
        "selectedMemoCount": len(memos), "excludedOlderMemoCount": total - len(memos),
        "newestCreated": memos[0]["created_at"] if memos else None,
        "oldestSelectedCreated": memos[-1]["created_at"] if memos else None,
        "selectedBytes": sum(len((memo["content"] or "").encode("utf-8")) for memo in memos),
        "sourceHashes": hashes,
    }
    try:
        rollup = build_rollup(snapshots)
        nodes, pending = [], list(rollup["groups"])
        while pending:
            node = pending.pop()
            nodes.append(node)
            pending.extend(node["children"])
        actionable = [node for node in nodes if node["kind"] == "task" and node["actionable"]]
        contexts = [node for node in nodes if not node["actionable"]]
        report["rollup"] = {
            "corpusDigest": rollup["corpusDigest"], "actionableGroups": len(actionable),
            "uncheckedSourceOccurrences": sum(sum(not item["checked"] for item in node["occurrences"]) for node in actionable),
            "duplicateGroups": sum(len(node["occurrences"]) > 1 for node in actionable),
            "contextGroups": len(contexts), "candidateSets": len(rollup["candidates"]),
            "rootGroups": len(rollup["groups"]),
        }
    except RollupValidationError as error:
        report["rollupError"] = {"code": error.code, "status": error.status_code}

    if args.snapshot:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path("/private/tmp") / f"todo-works-recent30-{stamp}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"project": project, "memos": snapshots, "report": report}, output, ensure_ascii=False, default=json_default)
        report["snapshotPath"] = str(path)
    print(json.dumps(report, ensure_ascii=False, default=json_default))


if __name__ == "__main__":
    main()
