import asyncio
import json
import logging
import secrets
from collections import deque
import psutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

import process_manager as pm
from database import AsyncSessionLocal
from models import Application, Node
from routers.nodes import (
    ensure_local_node, queue_node_command, wait_for_node_command,
    subscribe_node_stream, unsubscribe_node_stream,
    _node_ws_connections,
)

log = logging.getLogger("cloudbase.stats")
router = APIRouter(tags=["stats"])

_local_stats_history: deque = deque(maxlen=60)


@router.websocket("/ws/apps/{app_id}/stats")
async def stream_stats(app_id: int, websocket: WebSocket):
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Application).where(Application.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await websocket.send_json({"status": "stopped", "error": "App not found"})
            await websocket.close()
            return
        local_node = await ensure_local_node(db)
        node = await _get_app_node(app, db, local_node)

        if not node.is_local:
            caps = json.loads(node.capabilities or "{}")
            features = caps.get("features") or []
            agent_ws = _node_ws_connections.get(node.id)
            node_id = node.id
            app_id_val = app.id
            app_name_val = app.name

            # True WebSocket streaming if agent supports it and is connected
            if "streaming_stats" in features and agent_ws is not None:
                stream_id = secrets.token_hex(8)
                log.info("stats stream_stats via WS: app_id=%d node_id=%d stream_id=%s", app_id_val, node_id, stream_id)
                q = subscribe_node_stream(stream_id)
                frames = 0
                try:
                    await agent_ws.send_json({
                        "type": "command",
                        "command": {
                            "id": -1,
                            "command_type": "stream_stats",
                            "payload": {"app_id": app_id_val, "app_name": app_name_val, "stream_id": stream_id},
                        },
                    })
                    while True:
                        try:
                            data = await asyncio.wait_for(q.get(), timeout=30.0)
                            frames += 1
                            parsed = json.loads(data) if isinstance(data, str) else data
                            await websocket.send_json(parsed)
                        except asyncio.TimeoutError:
                            log.debug("stats WS stream_id=%s waiting for data (frames: %d)", stream_id, frames)
                except (WebSocketDisconnect, Exception) as e:
                    log.info("stats WS ended: stream_id=%s frames=%d reason=%s", stream_id, frames, e)
                finally:
                    unsubscribe_node_stream(stream_id, q)
                    try:
                        if _node_ws_connections.get(node_id) is agent_ws:
                            await agent_ws.send_json({"type": "cancel_stream", "stream_id": stream_id})
                    except Exception:
                        pass
                return

            # Fallback: poll every 2s — fresh DB session each iteration
            log.info("stats poll fallback: app_id=%d node_id=%d (no WS or streaming_stats not in caps=%s)",
                     app_id_val, node_id, features)
            try:
                while True:
                    async with AsyncSessionLocal() as poll_db:
                        cmd = await queue_node_command(
                            poll_db,
                            node_id=node_id,
                            app_id=app_id_val,
                            command_type="get_stats",
                            payload={"app_id": app_id_val, "app_name": app_name_val},
                        )
                        done = await wait_for_node_command(poll_db, cmd.id, timeout_seconds=20)
                    if done.status == "done":
                        payload = json.loads(done.result or "{}") if done.result else {}
                        await websocket.send_json(payload)
                    else:
                        log.warning("stats poll: cmd failed: %s", done.error_message)
                        await websocket.send_json({"status": "stopped", "error": done.error_message})
                    await asyncio.sleep(2)
            except (WebSocketDisconnect, Exception):
                pass
            return

    q = pm.subscribe_stats(app_id)

    # Flush stored history immediately so charts populate at once
    for point in pm.get_recent_stats(app_id):
        try:
            await websocket.send_json(point)
        except Exception:
            pm.unsubscribe_stats(app_id, q)
            return

    try:
        while True:
            data = await q.get()
            await websocket.send_json(data)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        pm.unsubscribe_stats(app_id, q)


async def _get_app_node(app: Application, db, local_node: Node) -> Node:
    if app.node_id:
        result = await db.execute(select(Node).where(Node.id == app.node_id))
        node = result.scalar_one_or_none()
        if node:
            return node
    return local_node


@router.websocket("/ws/system/stats")
async def stream_system_stats(websocket: WebSocket):
    await websocket.accept()
    try:
        # Replay history so charts populate immediately on connect
        for snapshot in list(_local_stats_history):
            await websocket.send_json(snapshot)

        psutil.cpu_percent(interval=None)
        while True:
            import time
            timestamp = int(time.time() * 1000)  # milliseconds
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
            payload = {
                "timestamp": timestamp,
                "cpu_percent": cpu,
                "memory_total_mb": round(mem.total / 1024 / 1024),
                "memory_used_mb": round(mem.used / 1024 / 1024),
                "memory_percent": mem.percent,
                "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
                "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                "disk_percent": disk.percent,
            }
            _local_stats_history.append(payload)
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
