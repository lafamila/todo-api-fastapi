import pymysql.err
import uvicorn
from fastapi import FastAPI, Response, status, BackgroundTasks, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from .connectors import Connector
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, Annotated, List
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Project(BaseModel):
    id: Optional[int] = None
    name: str
    icon: str


class Task(BaseModel):
    id: Optional[int] = None
    title: str = None
    task_status: int = 0


class MultiTasks(BaseModel):
    tasks: List[Task]


class Detail(BaseModel):
    content: str


@app.get("/health")
async def health_check():
    try:
        with Connector() as conn:
            curs = conn.cursor()
            curs.execute("SELECT 1")
            curs.fetchone()
    except Exception as e:
        import os

        return Response(
            content=f"Database connection error: {str(e)} {os.getenv('DB_SCHEME')}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return {"status": "ok"}


@app.get("/project")
async def get_all_projects():
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "SELECT project_id AS id, project_name AS name, project_icon AS icon FROM project WHERE project_status=0 ORDER BY reg_dtm"
        )
        result = curs.fetchall()
    return result


@app.get("/project/{project_id}")
async def get_project(project_id: int):
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "SELECT project_id AS id, project_name AS name, project_icon AS icon FROM project WHERE project_id=%s",
            project_id,
        )
        result = curs.fetchone()
    return result


@app.get("/project/{project_id}/task")
async def get_all_tasks(project_id: int):
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "SELECT task_id AS id, project_id, task_title AS title, DATE_FORMAT(reg_dtm, '%%y/%%m/%%d') AS reg_dtm, task_status FROM task t WHERE project_id=%s ORDER BY task_status, t.reg_dtm",
            project_id,
        )
        result = curs.fetchall()
    print(result)
    return result


@app.get("/project/{project_id}/task/{task_id}")
async def get_task(project_id: int, task_id: int):
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "SELECT content FROM detail WHERE task_id=%s ORDER BY reg_dtm DESC", task_id
        )
        result = curs.fetchone()
    return result


@app.post("/project/{project_id}/task/{task_id}")
async def post_detail(project_id: int, task_id: int, detail: Detail):
    detail_data = detail.model_dump()
    detail_data["task_id"] = task_id
    with Connector() as conn:
        curs = conn.cursor()
        try:
            curs.execute(
                "INSERT INTO detail(task_id, content) VALUES (%(task_id)s, %(content)s)",
                detail_data,
            )
        except pymysql.err.IntegrityError as e:
            pass
    return {"status": True}


@app.post("/project/{project_id}/task")
async def post_task(project_id: int, task: Task):
    task_data = task.model_dump()
    task_data["project_id"] = project_id
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "INSERT INTO task(project_id, task_title) VALUES (%(project_id)s, %(title)s)",
            task_data,
        )
        task_id = curs.lastrowid
        curs.execute(
            "SELECT task_id AS id, project_id, task_title AS title, DATE_FORMAT(reg_dtm, '%%y/%%m/%%d') AS reg_dtm, task_status FROM task WHERE task_id=%s",
            task_id,
        )
        result = curs.fetchone()

    return result


@app.patch("/project/{project_id}/task")
async def update_tasks(project_id: int, tasks: MultiTasks):
    tasks_data = tasks.model_dump()
    with Connector() as conn:
        curs = conn.cursor()
        curs.executemany(
            "UPDATE task SET task_status=%(task_status)s WHERE task_id=%(id)s",
            tasks_data["tasks"],
        )

    return ""


@app.post("/project")
async def post_project(project: Project):
    print(project.model_dump())
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute(
            "INSERT INTO project(project_name, project_icon) VALUES (%(name)s, %(icon)s)",
            project.model_dump(),
        )
        project_id = curs.lastrowid
    project.id = project_id
    return project.model_dump()


if __name__ == "__main__":
    with Connector() as conn:
        curs = conn.cursor()
        curs.execute("SELECT * FROM project WHERE 1=1")
        data = curs.fetchall()
    print(data)
    uvicorn.run(app, host="0.0.0.0", port=20022)
