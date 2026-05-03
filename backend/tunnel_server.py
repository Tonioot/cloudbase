"""Reverse WebSocket tunnel server — TCP-over-WebSocket multiplexing.

Architecture:
  Remote node agent connects via WS to /api/nodes/ws/tunnel/{replica_id}.
  The main node allocates a free local TCP port (9100-9999) and starts an
  asyncio TCP server on 127.0.0.1:{port}.  Nginx is then reconfigured to
  proxy_pass to that localhost port instead of the remote node's public IP.

Protocol (JSON text frames over the tunnel WebSocket):

  Main node → Agent:
    {"type": "connect", "conn_id": "<8 hex chars>"}   — open TCP conn to replica
    {"type": "data",    "conn_id": "...", "data": "<base64>"}
    {"type": "close",   "conn_id": "..."}

  Agent → Main node:
    {"type": "data",   "conn_id": "...", "data": "<base64>"}
    {"type": "close",  "conn_id": "..."}

No firewall ports need to be opened on the remote node.  The agent initiates
all connections (outbound from Node B → Main), which is nearly always allowed.
"""

import asyncio
import base64
import json
import logging
import secrets
from typing import Optional

log = logging.getLogger("cloudbase.tunnel")

TUNNEL_PORT_MIN = 9100
TUNNEL_PORT_MAX = 9999

# replica_id → _TunnelEntry
_tunnels: dict[int, "_TunnelEntry"] = {}


class _TunnelEntry:
    """One active tunnel: nginx → local TCP listener → WS → agent → replica container."""

    def __init__(self, replica_id: int, local_port: int, ws) -> None:
        self.replica_id = replica_id
        self.local_port = local_port
        self.ws = ws  # FastAPI WebSocket
        self._server: Optional[asyncio.Server] = None
        # conn_id → (asyncio.StreamReader, asyncio.StreamWriter)
        self._conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept_conn, "127.0.0.1", self.local_port
        )
        asyncio.get_running_loop().create_task(self._server.serve_forever())
        log.info("[tunnel] replica=%d listening on 127.0.0.1:%d", self.replica_id, self.local_port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except Exception:
                pass
            self._server = None
        for _, writer in list(self._conns.values()):
            try:
                writer.close()
            except Exception:
                pass
        self._conns.clear()
        log.info("[tunnel] replica=%d stopped", self.replica_id)

    async def _accept_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Called by asyncio when nginx opens a new TCP connection to the tunnel port."""
        conn_id = secrets.token_hex(8)
        self._conns[conn_id] = (reader, writer)
        try:
            await self.ws.send_text(json.dumps({"type": "connect", "conn_id": conn_id}))
            asyncio.get_running_loop().create_task(self._tcp_to_ws(conn_id, reader))
        except Exception as e:
            log.warning(
                "[tunnel] replica=%d send connect failed conn_id=%s: %s",
                self.replica_id, conn_id, e,
            )
            self._conns.pop(conn_id, None)
            try:
                writer.close()
            except Exception:
                pass

    async def _tcp_to_ws(self, conn_id: str, reader: asyncio.StreamReader) -> None:
        """Forward bytes from the local TCP connection to the agent over WebSocket."""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await self.ws.send_text(
                    json.dumps({
                        "type": "data",
                        "conn_id": conn_id,
                        "data": base64.b64encode(chunk).decode(),
                    })
                )
        except Exception:
            pass
        finally:
            # Notify agent that this side closed
            try:
                await self.ws.send_text(json.dumps({"type": "close", "conn_id": conn_id}))
            except Exception:
                pass
            _, writer = self._conns.pop(conn_id, (None, None))
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass

    async def dispatch(self, raw: str) -> None:
        """Route one incoming message from the agent to the correct local TCP connection."""
        try:
            msg = json.loads(raw)
        except Exception:
            return
        msg_type = msg.get("type")
        conn_id = msg.get("conn_id")
        if not conn_id:
            return

        if msg_type == "data":
            pair = self._conns.get(conn_id)
            if pair:
                _, writer = pair
                try:
                    writer.write(base64.b64decode(msg["data"]))
                    await writer.drain()
                except Exception:
                    self._conns.pop(conn_id, None)
        elif msg_type == "close":
            pair = self._conns.pop(conn_id, None)
            if pair:
                _, writer = pair
                try:
                    writer.close()
                except Exception:
                    pass


# ── Public API ────────────────────────────────────────────────────────────────

def _next_free_port() -> Optional[int]:
    used = {e.local_port for e in _tunnels.values()}
    for port in range(TUNNEL_PORT_MIN, TUNNEL_PORT_MAX + 1):
        if port not in used:
            return port
    return None


async def open_tunnel(replica_id: int, ws) -> Optional[int]:
    """Register an agent tunnel WS and start the local TCP listener.

    Returns the allocated local_port, or None if no port is available.
    If a tunnel already exists for this replica_id it is replaced (reconnect).
    """
    if replica_id in _tunnels:
        await close_tunnel(replica_id)
    port = _next_free_port()
    if port is None:
        log.error("[tunnel] No free ports in range %d-%d", TUNNEL_PORT_MIN, TUNNEL_PORT_MAX)
        return None
    entry = _TunnelEntry(replica_id, port, ws)
    _tunnels[replica_id] = entry
    await entry.start()
    return port


async def close_tunnel(replica_id: int) -> None:
    """Tear down the tunnel and TCP listener for this replica."""
    entry = _tunnels.pop(replica_id, None)
    if entry:
        await entry.stop()


async def dispatch_message(replica_id: int, raw: str) -> None:
    """Route a message from the agent to the correct tunnel entry."""
    entry = _tunnels.get(replica_id)
    if entry:
        await entry.dispatch(raw)


def get_tunnel_port(replica_id: int) -> Optional[int]:
    entry = _tunnels.get(replica_id)
    return entry.local_port if entry else None


def is_active_tunnel(replica_id: int, ws) -> bool:
    """Return True only if *ws* is the currently registered tunnel websocket."""
    entry = _tunnels.get(replica_id)
    return entry is not None and entry.ws is ws


def active_tunnel_count() -> int:
    return len(_tunnels)
