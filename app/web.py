from __future__ import annotations

import asyncio
import re
from email.utils import formatdate
from urllib.parse import quote

from aiohttp import web

from .file_store import FileStore, safe_filename


RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _range_bounds(value: str, size: int) -> tuple[int, int] | None:
    match = RANGE_RE.fullmatch(value.strip())
    if not match or "," in value:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix <= 0 or size == 0:
            return None
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _content_disposition(filename: str) -> str:
    filename = safe_filename(filename)
    ascii_name = "".join(char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


async def _download(request: web.Request) -> web.StreamResponse:
    store: FileStore = request.app["store"]
    token = request.match_info["token"]
    item = store.get(token)
    if item is None:
        raise web.HTTPNotFound(text="This download link is invalid or has expired.")
    path = store.path_for(item)
    if not path.is_file():
        store.delete(token)
        raise web.HTTPNotFound(text="This file is no longer available.")

    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    range_header = request.headers.get("Range")
    if range_header and not request.headers.get("If-Range"):
        bounds = _range_bounds(range_header, size)
        if bounds is None:
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start, end = bounds
        status = 206

    content_type = item["content_type"] or "application/octet-stream"
    if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type):
        content_type = "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(max(0, end - start + 1)),
        "Content-Disposition": _content_disposition(item["filename"]),
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
        "Last-Modified": formatdate(item["created_at"], usegmt=True),
        "Cache-Control": "private, max-age=300",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)
    if request.method == "HEAD" or size == 0:
        await response.write_eof()
        return response

    remaining = end - start + 1
    try:
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                block = await asyncio.to_thread(source.read, min(256 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                await response.write(block)
        await response.write_eof()
    except (ConnectionResetError, BrokenPipeError):
        pass
    return response


def create_web_app(store: FileStore) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app["store"] = store
    app.router.add_get("/health", _health)
    app.router.add_route("GET", "/d/{token}", _download)
    app.router.add_route("HEAD", "/d/{token}", _download)
    return app
