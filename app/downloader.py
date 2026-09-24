from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp


class DownloadError(Exception):
    """A safe, user-displayable download failure."""


class RangeNotSupported(Exception):
    pass


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve only globally routable addresses to prevent SSRF to local networks."""

    def __init__(self) -> None:
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not literal.is_global:
                raise OSError("Private or reserved IP addresses are not allowed")
            return await self._resolver.resolve(host, port, family)

        records = await self._resolver.resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(record["host"]).is_global for record in records):
            raise OSError("Host resolves to a private or reserved IP address")
        return records

    async def close(self) -> None:
        await self._resolver.close()


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise DownloadError("That link is not a valid web URL.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise DownloadError("Send a direct HTTP or HTTPS file link.")
    if parsed.username is not None or parsed.password is not None:
        raise DownloadError("Links containing a username or password are not supported.")


def _safe_filename(response: aiohttp.ClientResponse, url: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", disposition, re.I)
    if match:
        name = unquote(match.group(1).strip().strip('"\''))
    else:
        match = re.search(r'filename\s*=\s*"?([^";]+)', disposition, re.I)
        name = match.group(1).strip() if match else ""
    if not name:
        name = unquote(Path(urlsplit(url).path).name) or "download"
    name = name.replace("\\", "_").replace("/", "_").replace("\x00", "")
    name = "".join(ch for ch in name if ch.isprintable()).strip(" .")
    return (name or "download")[:180]


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip(), re.I)
    return tuple(map(int, match.groups())) if match else None


async def _request_following_redirects(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_redirects: int = 5,
) -> aiohttp.ClientResponse:
    current = url
    for _ in range(max_redirects + 1):
        _validate_url(current)
        response = await session.request(method, current, headers=headers, allow_redirects=False)
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        response.release()
        if not location:
            raise DownloadError("The file host returned an invalid redirect.")
        current = urljoin(current, location)
    raise DownloadError("The file link redirected too many times.")


async def _probe(session: aiohttp.ClientSession, url: str) -> tuple[str, str, int | None, bool]:
    head = await _request_following_redirects(session, "HEAD", url)
    try:
        if head.status >= 400 and head.status not in {403, 405, 501}:
            raise DownloadError(f"The file host returned HTTP {head.status}.")
        final_url = str(head.url)
        filename = _safe_filename(head, final_url)
        size: int | None = None
        try:
            size = int(head.headers.get("Content-Length", ""))
        except ValueError:
            pass
    finally:
        head.release()

    probe = await _request_following_redirects(session, "GET", final_url, headers={"Range": "bytes=0-0"})
    try:
        if probe.status >= 400:
            raise DownloadError(f"The file host returned HTTP {probe.status}.")
        final_url = str(probe.url)
        filename = _safe_filename(probe, final_url) or filename
        content_range = _content_range(probe.headers.get("Content-Range"))
        if probe.status == 206 and content_range and content_range[0:2] == (0, 0):
            size = content_range[2]
            return final_url, filename, size, size > 0
        if probe.status == 200:
            try:
                size = int(probe.headers.get("Content-Length", ""))
            except ValueError:
                pass
            return final_url, filename, size, False
        return final_url, filename, size, False
    finally:
        probe.release()


async def _write_stream(response: aiohttp.ClientResponse, target: Path, limit: int) -> int:
    written = 0
    with target.open("wb") as output:
        async for block in response.content.iter_chunked(256 * 1024):
            written += len(block)
            if written > limit:
                raise DownloadError("The file is larger than this bot's configured size limit.")
            output.write(block)
    return written


async def _download_serial(
    session: aiohttp.ClientSession, url: str, target: Path, limit: int, expected_size: int | None
) -> None:
    response = await _request_following_redirects(session, "GET", url)
    try:
        if response.status >= 400:
            raise DownloadError(f"The file host returned HTTP {response.status}.")
        written = await _write_stream(response, target, limit)
        if expected_size is not None and written != expected_size:
            raise DownloadError("The download ended early. Try the link again.")
    finally:
        response.release()


async def _download_part(
    session: aiohttp.ClientSession, url: str, target: Path, start: int, end: int, limit: int
) -> None:
    response = await _request_following_redirects(
        session, "GET", url, headers={"Range": f"bytes={start}-{end}"}
    )
    try:
        content_range = _content_range(response.headers.get("Content-Range"))
        if response.status != 206 or content_range != (start, end, limit):
            raise RangeNotSupported()
        written = await _write_stream(response, target, end - start + 1)
        if written != end - start + 1:
            raise DownloadError("A download part ended early. Try the link again.")
    finally:
        response.release()


async def download_file(
    url: str,
    *,
    max_size_bytes: int,
    max_parallel_ranges: int,
    work_dir: str | None = None,
) -> tuple[Path, str, tempfile.TemporaryDirectory[str]]:
    """Download a public URL into a temporary directory and return its path and filename."""
    _validate_url(url)
    temporary = tempfile.TemporaryDirectory(prefix="linkdrop-", dir=work_dir)
    root = Path(temporary.name)
    destination = root / "download.bin"
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=max(2, max_parallel_ranges + 2))
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=90)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, auto_decompress=False) as session:
            final_url, filename, size, supports_ranges = await _probe(session, url)
            if size is not None and size > max_size_bytes:
                raise DownloadError("That file is larger than this bot's configured size limit.")

            if supports_ranges and size is not None and max_parallel_ranges > 1:
                count = min(max_parallel_ranges, size)
                part_size = (size + count - 1) // count
                parts = [root / f"part-{index:02d}" for index in range(count)]
                jobs = []
                for index, part in enumerate(parts):
                    start = index * part_size
                    end = min(size - 1, start + part_size - 1)
                    jobs.append(_download_part(session, final_url, part, start, end, size))
                results = await asyncio.gather(*jobs, return_exceptions=True)
                failure = next((item for item in results if isinstance(item, BaseException)), None)
                if failure is None:
                    with destination.open("wb") as output:
                        for part in parts:
                            with part.open("rb") as piece:
                                shutil.copyfileobj(piece, output, length=1024 * 1024)
                    for part in parts:
                        part.unlink(missing_ok=True)
                elif isinstance(failure, RangeNotSupported):
                    for part in parts:
                        part.unlink(missing_ok=True)
                    await _download_serial(session, final_url, destination, max_size_bytes, size)
                else:
                    raise failure
            else:
                await _download_serial(session, final_url, destination, max_size_bytes, size)

            actual_size = destination.stat().st_size
            if actual_size > max_size_bytes:
                raise DownloadError("That file is larger than this bot's configured size limit.")
            if size is not None and actual_size != size:
                raise DownloadError("The download was incomplete. Try the link again.")
        return destination, filename, temporary
    except DownloadError:
        temporary.cleanup()
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        temporary.cleanup()
        raise DownloadError("Could not download that link. It may be unavailable or require a login.") from exc
    except BaseException:
        temporary.cleanup()
        raise
