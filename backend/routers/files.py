import logging
import os
import mimetypes
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Application, Node
import process_manager as pm
from routers.nodes import ensure_local_node, queue_node_command, wait_for_node_command

log = logging.getLogger("cloudbase.files")

router = APIRouter(prefix="/api/apps", tags=["files"])


def _resolve_target_path(base_dir: str, path: str) -> tuple[str, str]:
    base_abs = os.path.abspath(base_dir)
    relative = (path or "").lstrip("/\\")
    target_abs = os.path.abspath(os.path.join(base_abs, relative))
    try:
        if os.path.commonpath([base_abs, target_abs]) != base_abs:
            raise HTTPException(400, "Path traversal not allowed")
    except ValueError:
        raise HTTPException(400, "Path traversal not allowed")
    return base_abs, target_abs


@router.get("/{app_id}/files")
async def list_files(
    app_id: int,
    path: str = Query("", description="Relative path inside the app directory"),
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status != "online":
            raise HTTPException(503, "Node is offline")
        log.info("list_files: app_id=%d node_id=%d path=%r", app.id, node.id, path)
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="list_files",
            payload={"app_id": app.id, "app_name": app.name, "path": path},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            log.warning("list_files: cmd=%d failed: %s", cmd.id, done.error_message)
            raise HTTPException(502, done.error_message or "Remote file listing failed")
        result = json.loads(done.result or "{}") if done.result else {}
        log.info("list_files: cmd=%d done, %d entries", cmd.id, len(result.get("entries", [])))
        return {
            "path": result.get("path", path or "."),
            "entries": result.get("entries", []) or [],
        }

    base_dir, target = _resolve_target_path(app.working_dir or pm.get_app_dir(app.name), path)

    if not os.path.exists(target):
        raise HTTPException(404, "Path not found")

    if os.path.isfile(target):
        raise HTTPException(400, "Path is a file, not a directory")

    entries = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        stat = os.stat(full)
        entries.append({
            "name": name,
            "path": os.path.relpath(full, base_dir),
            "is_dir": os.path.isdir(full),
            "size": stat.st_size if os.path.isfile(full) else None,
            "modified": stat.st_mtime,
        })

    return {"path": os.path.relpath(target, base_dir), "entries": entries}


@router.get("/{app_id}/files/content")
async def get_file_content(
    app_id: int,
    path: str = Query(..., description="Relative file path"),
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status != "online":
            raise HTTPException(503, "Node is offline")
        log.info("get_file_content: app_id=%d node_id=%d path=%r", app.id, node.id, path)
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_file_content",
            payload={"app_id": app.id, "app_name": app.name, "path": path},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            log.warning("get_file_content: cmd=%d failed: %s", cmd.id, done.error_message)
            raise HTTPException(502, done.error_message or "Remote file read failed")
        result = json.loads(done.result or "{}") if done.result else {}
        log.info("get_file_content: cmd=%d done, binary=%s", cmd.id, result.get("binary"))
        return {
            "path": result.get("path", path),
            "content": result.get("content"),
            "binary": bool(result.get("binary", False)),
            "mime": result.get("mime") or "text/plain",
        }

    base_dir, target = _resolve_target_path(app.working_dir or pm.get_app_dir(app.name), path)

    if not os.path.isfile(target):
        raise HTTPException(404, "File not found")

    size = os.path.getsize(target)
    if size > 1_000_000:
        raise HTTPException(413, "File too large to display (>1MB)")

    mime, _ = mimetypes.guess_type(target)
    basename = os.path.basename(target)
    _TEXT_EXTENSIONS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
        ".toml", ".env", ".sh", ".md", ".txt", ".css", ".html", ".xml",
        ".cfg", ".ini", ".conf", ".go", ".rs", ".rb", ".php", ".java",
        ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
        ".sql", ".graphql", ".proto", ".tf", ".hcl", ".dockerfile",
        ".gitignore", ".editorconfig", ".prettierrc", ".eslintrc",
        ".babelrc", ".nvmrc", ".npmrc",
    }
    _TEXT_BASENAMES = {
        "Dockerfile", "dockerfile", "Makefile", "makefile", "Procfile",
        "Pipfile", "Gemfile", "Vagrantfile", "Brewfile", "Justfile",
        ".env", ".env.local", ".env.example", ".env.production",
        ".gitignore", ".gitattributes", ".dockerignore", ".npmignore",
        ".editorconfig", ".nvmrc", ".npmrc", ".yarnrc",
        "requirements.txt", "package.json", "go.mod", "go.sum",
        "composer.json", "cargo.toml", "pyproject.toml",
    }
    _, ext = os.path.splitext(basename)
    is_text = (
        (mime and mime.startswith("text")) or
        ext.lower() in _TEXT_EXTENSIONS or
        basename in _TEXT_BASENAMES
    )

    if not is_text:
        return {"path": path, "content": None, "binary": True, "mime": mime}

    with open(target, "r", errors="replace") as f:
        content = f.read()

    return {"path": path, "content": content, "binary": False, "mime": mime or "text/plain"}


async def _get_or_404(app_id: int, db: AsyncSession) -> Application:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "App not found")
    return app


async def _get_app_node(app: Application, db: AsyncSession, local_node: Node) -> Node:
    if app.node_id:
        result = await db.execute(select(Node).where(Node.id == app.node_id))
        node = result.scalar_one_or_none()
        if node:
            return node
    return local_node
