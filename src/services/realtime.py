import logging
from collections import defaultdict

try:
    import socketio
except ImportError:  # pragma: no cover
    socketio = None


logger = logging.getLogger(__name__)


class MemoRealtimeServer:
    def __init__(self, session_service, allowed_origins: list[str]):
        self.session_service = session_service
        self.allowed_origins = allowed_origins
        self.rooms: dict[str, set[str]] = defaultdict(set)
        self.locks: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.socket_memos: dict[str, set[str]] = defaultdict(set)
        self.screen_shares: dict[str, dict] = {}
        self.socket_projects: dict[str, set[str]] = defaultdict(set)

        self.available = socketio is not None
        self.sio = None
        if not self.available:
            logger.warning("python-socketio is not installed; realtime server disabled")
            return

        self.sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins=allowed_origins,
            cors_credentials=True,
        )
        self._register_handlers()

    def wrap_asgi(self, other_asgi_app):
        if not self.available:
            return other_asgi_app
        return socketio.ASGIApp(self.sio, other_asgi_app, socketio_path="api/socket.io")

    def _register_handlers(self) -> None:
        @self.sio.event
        async def connect(sid, environ, auth):  # noqa: ARG001
            user = self.session_service.get_session_user_from_cookie_header(
                _extract_cookie_header(environ)
            )
            if not user:
                raise ConnectionRefusedError("Todo session is required")

            self.users[sid] = {
                "user_id": user["id"],
                "display_name": user.get("display_name") or user.get("username") or user["id"],
            }
            self.socket_memos[sid] = set()
            self.socket_projects[sid] = set()

        @self.sio.event
        async def disconnect(sid):
            for memo_id in list(self.socket_memos.get(sid, set())):
                await self._leave_memo(sid, memo_id)

            stopped_projects = []
            for project_id, share in list(self.screen_shares.items()):
                if share["socketId"] == sid:
                    self.screen_shares.pop(project_id, None)
                    stopped_projects.append(project_id)

            for project_id in stopped_projects:
                await self.sio.emit(
                    "screenShareStopped",
                    {"projectId": project_id},
                    room=f"project:{project_id}",
                )

            self.users.pop(sid, None)
            self.socket_memos.pop(sid, None)
            self.socket_projects.pop(sid, None)

        @self.sio.on("joinProject")
        async def join_project(sid, data):
            project_id = _get_id(data, "projectId")
            if not project_id:
                return
            room_name = f"project:{project_id}"
            await self.sio.enter_room(sid, room_name)
            self.socket_projects[sid].add(project_id)

            share = self.screen_shares.get(project_id)
            await self.sio.emit(
                "screenShareStatus",
                {
                    "projectId": project_id,
                    "isSharing": bool(share),
                    "sharer": (
                        {
                            "userId": share["userId"],
                            "displayName": share["displayName"],
                        }
                        if share
                        else None
                    ),
                },
                to=sid,
            )

        @self.sio.on("leaveProject")
        async def leave_project(sid, data):
            project_id = _get_id(data, "projectId")
            if not project_id:
                return
            await self.sio.leave_room(sid, f"project:{project_id}")
            self.socket_projects[sid].discard(project_id)

            share = self.screen_shares.get(project_id)
            if share and share["socketId"] == sid:
                await self._stop_screen_share(project_id)

        @self.sio.on("startScreenShare")
        async def start_screen_share(sid, data):
            project_id = _get_id(data, "projectId")
            user_info = self.users.get(sid)
            if not project_id or not user_info:
                return

            existing = self.screen_shares.get(project_id)
            if existing and existing["socketId"] != sid:
                await self.sio.emit("screenShareDenied", {"projectId": project_id}, to=sid)
                return

            self.screen_shares[project_id] = {
                "userId": user_info["user_id"],
                "displayName": user_info["display_name"],
                "socketId": sid,
            }
            await self.sio.emit(
                "screenShareStarted",
                {
                    "projectId": project_id,
                    "sharer": {
                        "userId": user_info["user_id"],
                        "displayName": user_info["display_name"],
                    },
                },
                room=f"project:{project_id}",
            )

        @self.sio.on("stopScreenShare")
        async def stop_screen_share(sid, data):
            project_id = _get_id(data, "projectId")
            if not project_id:
                return
            share = self.screen_shares.get(project_id)
            if not share or share["socketId"] != sid:
                return
            await self._stop_screen_share(project_id)

        @self.sio.on("joinMemo")
        async def join_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id:
                return
            room_name = f"memo:{memo_id}"
            await self.sio.enter_room(sid, room_name)
            self.rooms[memo_id].add(sid)
            self.socket_memos[sid].add(memo_id)

            lock = self.locks.get(memo_id)
            await self.sio.emit(
                "lockStatus",
                {
                    "memoId": memo_id,
                    "lockedBy": lock["displayName"] if lock else None,
                    "lockedByUserId": lock["userId"] if lock else None,
                },
                to=sid,
            )

        @self.sio.on("leaveMemo")
        async def leave_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if memo_id:
                await self._leave_memo(sid, memo_id)

        @self.sio.on("lockMemo")
        async def lock_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            user_info = self.users.get(sid)
            if not memo_id or not user_info:
                return

            existing = self.locks.get(memo_id)
            if existing and existing["socketId"] != sid:
                await self.sio.emit(
                    "lockDenied",
                    {"memoId": memo_id, "lockedBy": existing["displayName"]},
                    to=sid,
                )
                return

            self.locks[memo_id] = {
                "socketId": sid,
                "displayName": user_info["display_name"],
                "userId": user_info["user_id"],
            }
            await self.sio.emit(
                "memoLocked",
                {
                    "memoId": memo_id,
                    "displayName": user_info["display_name"],
                    "userId": user_info["user_id"],
                },
                room=f"memo:{memo_id}",
                skip_sid=sid,
            )
            await self.sio.emit("lockAcquired", {"memoId": memo_id}, to=sid)

        @self.sio.on("unlockMemo")
        async def unlock_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id:
                return
            lock = self.locks.get(memo_id)
            if not lock or lock["socketId"] != sid:
                return
            self.locks.pop(memo_id, None)
            await self.sio.emit("memoUnlocked", {"memoId": memo_id}, room=f"memo:{memo_id}")

        @self.sio.on("memoUpdated")
        async def memo_updated(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id:
                return
            await self.sio.emit(
                "memoContentUpdated",
                {
                    "memoId": memo_id,
                    "content": data.get("content", ""),
                    "title": data.get("title"),
                },
                room=f"memo:{memo_id}",
                skip_sid=sid,
            )

    async def _leave_memo(self, sid: str, memo_id: str) -> None:
        room_name = f"memo:{memo_id}"
        await self.sio.leave_room(sid, room_name)
        self.rooms[memo_id].discard(sid)
        if not self.rooms[memo_id]:
            self.rooms.pop(memo_id, None)

        self.socket_memos[sid].discard(memo_id)
        lock = self.locks.get(memo_id)
        if lock and lock["socketId"] == sid:
            self.locks.pop(memo_id, None)
            await self.sio.emit("memoUnlocked", {"memoId": memo_id}, room=room_name)

    async def _stop_screen_share(self, project_id: str) -> None:
        self.screen_shares.pop(project_id, None)
        await self.sio.emit(
            "screenShareStopped",
            {"projectId": project_id},
            room=f"project:{project_id}",
        )


def _extract_cookie_header(environ: dict) -> str | None:
    scope = environ.get("asgi.scope") or {}
    headers = scope.get("headers") or []
    for key, value in headers:
        if key.decode("latin1").lower() == "cookie":
            return value.decode("latin1")
    return environ.get("HTTP_COOKIE")


def _get_id(data: dict | None, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return str(value) if value else None
