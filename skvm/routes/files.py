"""File manager API — browse, read, write, upload, download, search, share."""

from __future__ import annotations

import logging
import os
import posixpath
import secrets
from datetime import timedelta

from flask import Blueprint, Response, current_app, render_template, request, send_file

from ..backends import BackendError, get_backend
from ..db import db, iso, utcnow
from ..models import FileEntry, Role, Server
from ..security import current_user, login_required
from ..services import audit
from ..services.settings import settings
from .helpers import arg, body, fail, get_server, ok

log = logging.getLogger("skvm.files")

bp = Blueprint("files", __name__, url_prefix="/files")

TEXT_EXTENSIONS = {
    ".txt", ".log", ".conf", ".cfg", ".ini", ".env", ".sh", ".bash", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".toml", ".md", ".html", ".htm", ".css", ".xml", ".sql", ".service", ".rules",
    ".htaccess", ".pem", ".crt", ".key", ".pub", ".c", ".h", ".cpp", ".go", ".rs", ".rb", ".php",
    ".csv", ".tsv", ".sock", ".lock", ".list", ".repo", ".colors", ".desktop",
}
MAX_UPLOAD_MB = 64
MAX_TEXT_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def _fs(uid: str):
    server = get_server(uid)
    backend = get_backend(server)
    try:
        backend.require("files")
    except BackendError as exc:
        raise BackendError(str(exc), code="not_supported") from exc
    return server, backend.filesystem(server.container_id)


def _safe_path(raw: str | None, default: str = "/root") -> str:
    path = (raw or default).strip() or default
    if not path.startswith("/"):
        path = posixpath.normpath(posixpath.join(default, path))
    normalised = posixpath.normpath(path)
    if not normalised.startswith("/"):
        normalised = "/" + normalised
    return normalised


def _serialise(entry: dict, is_text: bool | None = None) -> dict:
    name = entry.get("name") or ""
    extension = os.path.splitext(name)[1].lower()
    return {
        **entry,
        "extension": extension,
        "is_text": (extension in TEXT_EXTENSIONS) if is_text is None else is_text,
        "size_human": _human(entry.get("size") or 0),
        "modified_iso": iso(utcnow()) if not entry.get("modified") else None,
        "modified_human": _relative(entry.get("modified") or 0),
    }


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _relative(timestamp: float) -> str:
    if not timestamp:
        return "—"
    import time as _time

    delta = max(0, int(_time.time() - timestamp))
    for limit, unit in ((60, "s"), (3600, "m"), (86400, "h"), (2592000, "d"), (31536000, "mo")):
        if delta < limit:
            divisor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "mo": 2592000}[unit]
            return f"{delta // divisor}{unit} ago"
    return f"{delta // 31536000}y ago"


@bp.route("/<uid>/list")
@login_required
def list_dir(uid: str):
    path = _safe_path(arg("path", "/root"))
    try:
        server, fs = _fs(uid)
        entries = fs.list_dir(path)
        usage = fs.usage(path)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    return ok(
        path=path,
        parent=_safe_path(posixpath.dirname(path.rstrip("/")) or "/"),
        entries=[_serialise(entry) for entry in entries],
        usage=usage,
        breadcrumbs=_breadcrumbs(path),
    )


def _breadcrumbs(path: str) -> list[dict[str, str]]:
    crumbs = [{"name": "root", "path": "/"}]
    current = ""
    for part in [p for p in path.split("/") if p]:
        current += f"/{part}"
        crumbs.append({"name": part, "path": current})
    return crumbs


@bp.route("/<uid>/read")
@login_required
def read_file(uid: str):
    path = _safe_path(arg("path"))
    try:
        server, fs = _fs(uid)
        stat = fs.stat(path)
    except BackendError as exc:
        return fail(str(exc), 404 if exc.code == "not_found" else 400, code=exc.code)
    if stat.get("type") == "dir":
        return fail("That path is a directory", 400)
    if stat.get("size", 0) > MAX_TEXT_BYTES and not arg("force", False, bool):
        return fail(
            f"File is {_human(stat['size'])} — too large for the inline editor (1 MB limit). "
            "Download it instead, or pass force=1.",
            413,
            size=stat.get("size"),
        )
    try:
        data = fs.read(path, max_bytes=MAX_TEXT_BYTES if not arg("force", False, bool) else MAX_DOWNLOAD_BYTES)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    try:
        content = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        content = data[:4096].decode("utf-8", "replace")
        binary = True
    return ok(path=path, content=content, binary=binary, size=stat.get("size", len(data)), stat=_serialise(stat))


@bp.route("/<uid>/write", methods=["POST"])
@login_required
def write_file(uid: str):
    data = body()
    path = _safe_path(data.get("path"))
    content = data.get("content")
    if content is None:
        return fail("Missing 'content'", 400)
    if len(content.encode()) > MAX_TEXT_BYTES * 4:
        return fail("Content exceeds the 4 MB editor limit", 413)
    try:
        server, fs = _fs(uid)
        result = fs.write(path, content.encode(), mode=int(data.get("mode", 0o644)))
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.record("file.write", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"path": path, "bytes": len(content.encode())})
    audit.server_event(server, "file", f"Edited {path}")
    db.session.commit()
    return ok(result=result, message=f"Saved {posixpath.basename(path)}")


@bp.route("/<uid>/upload", methods=["POST"])
@login_required
def upload(uid: str):
    directory = _safe_path(request.form.get("path") or "/root")
    uploads = request.files.getlist("file") or request.files.getlist("files")
    if not uploads:
        return fail("No files in the request", 400)
    if len(uploads) > current_app.config.get("MAX_UPLOAD_FILES", 25):
        return fail("Too many files in one request", 413)
    limit = MAX_UPLOAD_MB * 1024 * 1024
    results = []
    try:
        server, fs = _fs(uid)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    for storage in uploads:
        name = os.path.basename(storage.filename or "upload.bin")[:120]
        if not name:
            continue
        payload = storage.read()
        if len(payload) > limit:
            results.append({"name": name, "error": f"larger than {MAX_UPLOAD_MB} MB"})
            continue
        target = posixpath.join(directory, name)
        try:
            fs.write(target, payload, mode=0o644)
            results.append({"name": name, "path": target, "size": len(payload)})
            db.session.add(
                FileEntry(server_id=server.id, path=target, size_bytes=len(payload),
                          uploaded_by=current_user().username)
            )
        except BackendError as exc:
            results.append({"name": name, "error": str(exc)})
    audit.record("file.upload", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"count": len(results), "directory": directory})
    audit.server_event(server, "file", f"Uploaded {len([r for r in results if 'error' not in r])} file(s) to {directory}")
    db.session.commit()
    return ok(results=results, message=f"Uploaded {len([r for r in results if 'error' not in r])} file(s)")


@bp.route("/<uid>/download")
@login_required
def download(uid: str):
    path = _safe_path(arg("path"))
    try:
        server, fs = _fs(uid)
        stat = fs.stat(path)
        if stat.get("size", 0) > MAX_DOWNLOAD_BYTES:
            return fail("File exceeds the 256 MB download limit", 413)
        payload = fs.read(path, max_bytes=MAX_DOWNLOAD_BYTES)
    except BackendError as exc:
        return fail(str(exc), 404 if exc.code == "not_found" else 400, code=exc.code)
    audit.record("file.download", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"path": path, "bytes": len(payload)})
    db.session.commit()
    filename = posixpath.basename(path) or "download.bin"
    return Response(
        payload,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/<uid>/mkdir", methods=["POST"])
@login_required
def mkdir(uid: str):
    data = body()
    path = _safe_path(data.get("path"))
    try:
        server, fs = _fs(uid)
        fs.mkdir(path, mode=int(data.get("mode", 0o755)))
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.server_event(server, "file", f"Created directory {path}")
    db.session.commit()
    return ok(path=path, message="Directory created")


@bp.route("/<uid>/delete", methods=["POST"])
@login_required
def delete(uid: str):
    data = body()
    path = _safe_path(data.get("path"))
    recursive = bool(data.get("recursive", False))
    if path in ("/", "/root", "/etc", "/usr", "/var", "/bin", "/boot"):
        return fail("Refusing to delete a system directory", 403)
    try:
        server, fs = _fs(uid)
        fs.delete(path, recursive=recursive)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.record("file.delete", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"path": path, "recursive": recursive}, result="warn")
    audit.server_event(server, "file", f"Deleted {path}", status="warn")
    db.session.commit()
    return ok(message=f"Deleted {posixpath.basename(path)}")


@bp.route("/<uid>/rename", methods=["POST"])
@login_required
def rename(uid: str):
    data = body()
    source = _safe_path(data.get("path"))
    destination = data.get("destination")
    if not destination:
        return fail("Missing 'destination'", 400)
    destination = _safe_path(destination) if destination.startswith("/") else posixpath.join(
        posixpath.dirname(source), destination
    )
    try:
        server, fs = _fs(uid)
        fs.rename(source, destination)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.server_event(server, "file", f"Renamed {source} → {destination}")
    db.session.commit()
    return ok(message="Renamed")


@bp.route("/<uid>/copy", methods=["POST"])
@login_required
def copy(uid: str):
    data = body()
    source = _safe_path(data.get("path"))
    destination = _safe_path(data.get("destination"))
    try:
        server, fs = _fs(uid)
        fs.copy(source, destination)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.server_event(server, "file", f"Copied {source} → {destination}")
    db.session.commit()
    return ok(message="Copied")


@bp.route("/<uid>/chmod", methods=["POST"])
@login_required
def chmod(uid: str):
    data = body()
    path = _safe_path(data.get("path"))
    mode = str(data.get("mode") or "").strip()
    if not mode or not all(ch in "01234567" for ch in mode):
        return fail("Provide an octal mode such as 0644", 400)
    try:
        server, fs = _fs(uid)
        fs.chmod(path, mode)
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    audit.server_event(server, "file", f"chmod {mode} {path}")
    db.session.commit()
    return ok(message=f"Permissions set to {mode}")


@bp.route("/<uid>/search")
@login_required
def search(uid: str):
    root = _safe_path(arg("root", "/"))
    pattern = arg("q", "*")
    if not pattern:
        return fail("Provide a search pattern", 400)
    try:
        server, fs = _fs(uid)
        rows = fs.search(root, pattern, limit=min(500, int(arg("limit", 200, int) or 200)))
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    except Exception as exc:  # drivers without search support
        return fail(f"Search is unavailable on this driver: {exc}", 501)
    return ok(results=[_serialise(row) for row in rows], root=root, pattern=pattern)


@bp.route("/<uid>/tail")
@login_required
def tail(uid: str):
    path = _safe_path(arg("path", "/var/log/syslog"))
    lines = min(2000, int(arg("lines", 200, int) or 200))
    try:
        server, fs = _fs(uid)
        content = fs.tail(path, lines=lines)
    except BackendError as exc:
        return fail(str(exc), 404 if exc.code == "not_found" else 400, code=exc.code)
    return ok(path=path, content=content, lines=lines)


@bp.route("/<uid>/share", methods=["POST"])
@login_required
def share(uid: str):
    """Create a time-limited download link for one file."""
    data = body()
    path = _safe_path(data.get("path"))
    hours = int(data.get("hours") or 24)
    server = get_server(uid)
    token = secrets.token_urlsafe(24)
    entry = FileEntry(
        server_id=server.id,
        path=path,
        size_bytes=0,
        uploaded_by=current_user().username,
        share_token=token,
        share_expires_at=utcnow() + timedelta(hours=max(1, min(720, hours))),
    )
    db.session.add(entry)
    audit.record("file.share", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"path": path, "hours": hours})
    db.session.commit()
    return ok(
        url=f"/share/{token}",
        expires_at=iso(entry.share_expires_at),
        message="Share link created",
    )


@bp.route("/share/<token>")
def shared_download(token: str):
    entry = db.session.query(FileEntry).filter(FileEntry.share_token == token).one_or_none()
    if entry is None:
        return render_template("error.html", code=404, title="Link not found",
                              message="This share link does not exist or was revoked."), 404
    expires = entry.share_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=utcnow().tzinfo)
    if expires is not None and expires < utcnow():
        return render_template("error.html", code=410, title="Link expired",
                              message="This share link has expired. Ask the owner for a fresh one."), 410
    server = db.session.get(Server, entry.server_id)
    if server is None:
        return render_template("error.html", code=404, title="Server gone",
                              message="The server that hosted this file no longer exists."), 404
    backend = get_backend(server)
    try:
        filesystem = backend.filesystem(server.container_id)
        payload = filesystem.read(entry.path, max_bytes=MAX_DOWNLOAD_BYTES)
    except BackendError as exc:
        return render_template("error.html", code=409, title="Unavailable",
                              message=f"The file could not be read: {exc}"), 409
    filename = posixpath.basename(entry.path) or "download.bin"
    audit.record("file.share_download", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"path": entry.path, "token": token[:8]})
    db.session.commit()
    return Response(
        payload,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/<uid>/picker")
@login_required
def picker(uid: str):
    """Directory picker used inside the wizard dialogs."""
    path = _safe_path(arg("path", "/"))
    try:
        server, fs = _fs(uid)
        entries = [entry for entry in fs.list_dir(path) if entry.get("type") == "dir"]
    except BackendError as exc:
        return fail(str(exc), 400, code=exc.code)
    return ok(path=path, directories=[_serialise(entry) for entry in entries],
              breadcrumbs=_breadcrumbs(path))
