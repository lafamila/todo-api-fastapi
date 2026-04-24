import calendar
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

try:
    from ..auth_utils import require_super_admin
    from ..connectors import get_db_connection
    from ..models.daily_tasks import (
        CompleteTaskRequest,
        CreateTaskTypeRequest,
        UpdateTaskTypeRequest,
    )
    from ..utils import generate_id
except ImportError:  # pragma: no cover
    from auth_utils import require_super_admin
    from connectors import get_db_connection
    from models.daily_tasks import (
        CompleteTaskRequest,
        CreateTaskTypeRequest,
        UpdateTaskTypeRequest,
    )
    from utils import generate_id


router = APIRouter(prefix="/api/daily-tasks", tags=["daily-tasks"])


def _task_type_to_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "icon": row["icon"] or "",
        "color": row["color"] or "#3994ef",
        "displayOrder": row["display_order"],
        "isActive": bool(row["is_active"]),
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def _get_active_count(cursor) -> int:
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM daily_task_types WHERE is_active = TRUE"
    )
    return cursor.fetchone()["cnt"]


def _sync_total_active_count(cursor, completed_date: str, active_count: int):
    """Update total_active_count for all completion rows on the given date."""
    cursor.execute(
        "UPDATE daily_task_completions SET total_active_count = %s WHERE completed_date = %s",
        (active_count, completed_date),
    )


# ─── Task Type CRUD ──────────────────────────────────────────────────────────


@router.post("/types")
async def create_task_type(
    data: CreateTaskTypeRequest, user: dict = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # Check limit
            cursor.execute("SELECT COUNT(*) AS cnt FROM daily_task_types")
            if cursor.fetchone()["cnt"] >= 50:
                raise HTTPException(
                    status_code=409, detail="Maximum 50 task types allowed"
                )

            # Check duplicate name
            cursor.execute(
                "SELECT id FROM daily_task_types WHERE name = %s", (data.name,)
            )
            if cursor.fetchone():
                raise HTTPException(
                    status_code=409, detail="Task type name already exists"
                )

            task_id = generate_id()
            # Get next display_order
            cursor.execute(
                "SELECT COALESCE(MAX(display_order), -1) + 1 AS next_order FROM daily_task_types"
            )
            next_order = cursor.fetchone()["next_order"]

            cursor.execute(
                """
                INSERT INTO daily_task_types (id, name, icon, color, display_order)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (task_id, data.name, data.icon, data.color, next_order),
            )

            cursor.execute(
                "SELECT * FROM daily_task_types WHERE id = %s", (task_id,)
            )
            return _task_type_to_dict(cursor.fetchone())


@router.get("/types")
async def get_task_types(user: dict = Depends(require_super_admin)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM daily_task_types ORDER BY display_order ASC, created_at ASC"
            )
            return [_task_type_to_dict(row) for row in cursor.fetchall()]


@router.put("/types/{type_id}")
async def update_task_type(
    type_id: str,
    data: UpdateTaskTypeRequest,
    user: dict = Depends(require_super_admin),
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM daily_task_types WHERE id = %s", (type_id,)
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Task type not found")

            updates = []
            values = []
            if data.name is not None:
                updates.append("name = %s")
                values.append(data.name)
            if data.icon is not None:
                updates.append("icon = %s")
                values.append(data.icon)
            if data.color is not None:
                updates.append("color = %s")
                values.append(data.color)
            if data.isActive is not None:
                updates.append("is_active = %s")
                values.append(data.isActive)
            if data.displayOrder is not None:
                updates.append("display_order = %s")
                values.append(data.displayOrder)

            if updates:
                values.append(type_id)
                cursor.execute(
                    f"UPDATE daily_task_types SET {', '.join(updates)} WHERE id = %s",
                    tuple(values),
                )

            cursor.execute(
                "SELECT * FROM daily_task_types WHERE id = %s", (type_id,)
            )
            return _task_type_to_dict(cursor.fetchone())


@router.delete("/types/{type_id}")
async def delete_task_type(
    type_id: str, user: dict = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM daily_task_types WHERE id = %s", (type_id,)
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Task type not found")

            cursor.execute(
                "UPDATE daily_task_types SET is_active = FALSE WHERE id = %s",
                (type_id,),
            )
            return {"message": "Task type deactivated"}


# ─── Completion ──────────────────────────────────────────────────────────────


@router.post("/complete")
async def complete_task(
    data: CompleteTaskRequest, user: dict = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # Verify task type exists and is active
            cursor.execute(
                "SELECT id FROM daily_task_types WHERE id = %s AND is_active = TRUE",
                (data.taskTypeId,),
            )
            if not cursor.fetchone():
                raise HTTPException(
                    status_code=404, detail="Active task type not found"
                )

            # Check if already completed
            cursor.execute(
                "SELECT id FROM daily_task_completions WHERE task_type_id = %s AND completed_date = %s",
                (data.taskTypeId, data.completedDate),
            )
            if cursor.fetchone():
                raise HTTPException(
                    status_code=409, detail="Task already completed for this date"
                )

            active_count = _get_active_count(cursor)
            comp_id = generate_id()

            cursor.execute(
                """
                INSERT INTO daily_task_completions (id, task_type_id, completed_date, total_active_count)
                VALUES (%s, %s, %s, %s)
                """,
                (comp_id, data.taskTypeId, data.completedDate, active_count),
            )

            # Sync all rows for this date
            _sync_total_active_count(cursor, data.completedDate, active_count)

            return {"message": "Task completed", "id": comp_id}


@router.delete("/complete/{task_type_id}/{completed_date}")
async def uncomplete_task(
    task_type_id: str,
    completed_date: str,
    user: dict = Depends(require_super_admin),
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM daily_task_completions WHERE task_type_id = %s AND completed_date = %s",
                (task_type_id, completed_date),
            )
            if not cursor.fetchone():
                raise HTTPException(
                    status_code=404, detail="Completion not found"
                )

            cursor.execute(
                "DELETE FROM daily_task_completions WHERE task_type_id = %s AND completed_date = %s",
                (task_type_id, completed_date),
            )

            # Recompute and sync remaining rows for this date
            active_count = _get_active_count(cursor)
            _sync_total_active_count(cursor, completed_date, active_count)

            return {"message": "Task uncompleted"}


# ─── Calendar (public) ───────────────────────────────────────────────────────


@router.get("/calendar")
async def get_calendar(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    days_in_month = calendar.monthrange(year, month)[1]
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{days_in_month:02d}"

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # Get completion data
            cursor.execute(
                """
                SELECT completed_date, COUNT(*) AS completed_count, MAX(total_active_count) AS total_count
                FROM daily_task_completions
                WHERE completed_date BETWEEN %s AND %s
                GROUP BY completed_date
                """,
                (start_date, end_date),
            )
            rows = cursor.fetchall()
            comp_map = {}
            for row in rows:
                d = str(row["completed_date"])
                comp_map[d] = {
                    "completedCount": row["completed_count"],
                    "totalCount": row["total_count"],
                }

            # Current active count for top-level info
            active_count = _get_active_count(cursor)

    # Build all days
    days = []
    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        if date_str in comp_map:
            cc = comp_map[date_str]["completedCount"]
            tc = comp_map[date_str]["totalCount"]
            ratio = round(cc / tc, 4) if tc > 0 else 0.0
            days.append({
                "date": date_str,
                "completedCount": cc,
                "totalCount": tc,
                "ratio": ratio,
            })
        else:
            days.append({
                "date": date_str,
                "completedCount": 0,
                "totalCount": 0,
                "ratio": 0.0,
            })

    return {
        "year": year,
        "month": month,
        "totalTaskTypes": active_count,
        "days": days,
    }


# ─── Day Detail (auth required) ─────────────────────────────────────────────


@router.get("/calendar/{target_date}")
async def get_day_detail(
    target_date: str, user: dict = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.id AS task_type_id, t.name, t.icon,
                       CASE WHEN c.id IS NOT NULL THEN TRUE ELSE FALSE END AS completed
                FROM daily_task_types t
                LEFT JOIN daily_task_completions c
                    ON c.task_type_id = t.id AND c.completed_date = %s
                WHERE t.is_active = TRUE
                ORDER BY t.display_order ASC, t.created_at ASC
                """,
                (target_date,),
            )
            tasks = [
                {
                    "taskTypeId": row["task_type_id"],
                    "name": row["name"],
                    "icon": row["icon"] or "",
                    "completed": bool(row["completed"]),
                }
                for row in cursor.fetchall()
            ]

    return {"date": target_date, "tasks": tasks}
