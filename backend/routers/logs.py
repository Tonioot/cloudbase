import asyncio
import json
import logging
import secrets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query

from database import AsyncSessionLocal, get_db
from models import Application, ApplicationReplica, Node
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import process_manager as pm
from routers.nodes import (
    ensure_local_node, queue_node_command, wait_for_node_command,
    subscribe_node_stream, unsubscribe_node_stream,
    _node_ws_connections,
)

log = logging.getLogger("cloudbase.logs")
router = APIRouter(tags=["logs"])


@router.get("/api/apps/{app_id}/logs/tail")
async def logs_tail(app_id: int, limit: int = Query(200, ge=1, le=2000), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if node.is_local:
        lines = pm.get_recent_logs(app_id, app.name)[-limit:]
        return {"lines": lines, "remote": False}

    if node.status != "online":
        return {"lines": [], "remote": True, "error": "Node is offline"}

    cmd = await queue_node_command(
        db,
        node_id=node.id,
        app_id=app.id,
        command_type="get_logs_tail",
        payload={"app_id": app.id, "app_name": app.name, "limit": limit},
    )
    done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
    if done.status != "done":
        return {"lines": [], "remote": True, "error": done.error_message}

    result = json.loads(done.result or "{}") if done.result else {}
    return {
        "lines": result.get("lines", []) or [],
        "remote": True,
    }


@router.websocket("/ws/apps/{app_id}/logs")
async def stream_logs(app_id: int, websocket: WebSocket):
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Application).where(Application.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await websocket.send_text("App not found\n")
            await websocket.close()
            return

        local_node = await ensure_local_node(db)
        node = await _get_app_node(app, db, local_node)

        if not node.is_local:
            import json as _json
            caps = _json.loads(node.capabilities or "{}")
            features = caps.get("features") or []
            agent_ws = _node_ws_connections.get(node.id)
            node_id = node.id
            app_id_val = app.id
            app_name_val = app.name

            # Use true WebSocket streaming if the agent supports it and is connected
            if "streaming_logs" in features and agent_ws is not None:
                stream_id = secrets.token_hex(8)
                log.info("logs stream_logs via WS: app_id=%d node_id=%d stream_id=%s", app_id_val, node_id, stream_id)
                q = subscribe_node_stream(stream_id)
                frames = 0
                try:
                    await agent_ws.send_json({
                        "type": "command",
                        "command": {
                            "id": -1,
                            "command_type": "stream_logs",
                            "payload": {"app_id": app_id_val, "app_name": app_name_val, "stream_id": stream_id},
                        },
                    })
                    while True:
                        try:
                            data = await asyncio.wait_for(q.get(), timeout=30.0)
                            frames += 1
                            await websocket.send_text(data if data.endswith("\n") else data + "\n")
                        except asyncio.TimeoutError:
                            log.debug("logs WS stream_id=%s waiting for data (frames so far: %d)", stream_id, frames)
                except (WebSocketDisconnect, Exception) as e:
                    log.info("logs WS ended: stream_id=%s frames=%d reason=%s", stream_id, frames, e)
                finally:
                    unsubscribe_node_stream(stream_id, q)
                    try:
                        if _node_ws_connections.get(node_id) is agent_ws:
                            await agent_ws.send_json({"type": "cancel_stream", "stream_id": stream_id})
                    except Exception:
                        pass
                return

            # Fallback: poll-and-diff — use a fresh DB session each iteration
            log.info("logs poll fallback: app_id=%d node_id=%d (no WS or streaming_logs not in caps=%s)",
                     app_id_val, node_id, features)
            last_snapshot: list[str] = []
            try:
                while True:
                    async with AsyncSessionLocal() as poll_db:
                        cmd = await queue_node_command(
                            poll_db,
                            node_id=node_id,
                            app_id=app_id_val,
                            command_type="get_logs_tail",
                            payload={"app_id": app_id_val, "app_name": app_name_val, "limit": 200},
                        )
                        done = await wait_for_node_command(poll_db, cmd.id, timeout_seconds=20)
                    if done.status == "done":
                        payload = _json.loads(done.result or "{}") if done.result else {}
                        lines = payload.get("lines", []) or []
                        if lines != last_snapshot:
                            if last_snapshot and len(lines) >= len(last_snapshot) and lines[:len(last_snapshot)] == last_snapshot:
                                delta = lines[len(last_snapshot):]
                            else:
                                delta = lines
                            for line in delta:
                                await websocket.send_text(line + "\n")
                            last_snapshot = lines
                    elif done.error_message:
                        await websocket.send_text(f"[remote-log-error] {done.error_message}\n")
                    await asyncio.sleep(2)
            except (WebSocketDisconnect, Exception):
                pass
            return

        # Local app: subscribe before snapshot so we don't miss lines produced during send
        q = pm.subscribe_logs(app_id)
        recent = pm.get_recent_logs(app_id, app.name)
        for line in recent:
            await websocket.send_text(line + "\n")

    try:
        while True:
            line = await q.get()
            await websocket.send_text(line + "\n")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        pm.unsubscribe_logs(app_id, q)


async def _get_or_404(app_id: int, db: AsyncSession) -> Application:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "App not found")
    return app


async def _get_app_node(app: Application, db: AsyncSession, local_node: Node) -> Node:
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
        .order_by(ApplicationReplica.status.desc()).limit(5)
    )
    replicas = rep_result.scalars().all()
    running = next((r for r in replicas if r.status in ("running", "starting") and r.node_id), None)
    candidate = running or next((r for r in replicas if r.node_id), None)
    if candidate and candidate.node_id:
        node_result = await db.execute(select(Node).where(Node.id == candidate.node_id))
        node = node_result.scalar_one_or_none()
        if node:
            return node
    return local_node
