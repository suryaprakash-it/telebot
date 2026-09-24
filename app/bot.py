from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from aiohttp import web

from .downloader import DownloadError, download_file
from .file_store import FileStore, safe_filename
from .web import create_web_app


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("linkdrop")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
MAX_FILE_SIZE_MB = max(1, int(os.getenv("MAX_FILE_SIZE_MB", "20")))
MAX_URL_DOWNLOAD_MB = max(1, int(os.getenv("MAX_URL_DOWNLOAD_MB", "49")))
MAX_RANGES = max(1, min(16, int(os.getenv("MAX_PARALLEL_RANGES", "8"))))
MAX_ACTIVE = max(1, min(10, int(os.getenv("MAX_ACTIVE_DOWNLOADS", "2"))))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_URL_DOWNLOAD_BYTES = MAX_URL_DOWNLOAD_MB * 1024 * 1024
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
LINK_TTL_HOURS = max(1, int(os.getenv("LINK_TTL_HOURS", "168")))
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,!?;:)]}"


class TelegramAPI:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self.session = session
        self.token = token
        self.base = f"{API_BASE}/bot{token}"

    async def call(self, method: str, payload: dict | None = None) -> dict:
        async with self.session.post(f"{self.base}/{method}", json=payload or {}) as response:
            data = await response.json(content_type=None)
            if not data.get("ok"):
                description = data.get("description", "Telegram API request failed")
                raise RuntimeError(f"Telegram {method} failed: {description}")
            return data.get("result", {})

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_file(self, chat_id: int, path: Path, filename: str) -> None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        with path.open("rb") as file:
            form.add_field("document", file, filename=filename, content_type="application/octet-stream")
            async with self.session.post(f"{self.base}/sendDocument", data=form) as response:
                data = await response.json(content_type=None)
                if not data.get("ok"):
                    description = data.get("description", "Telegram could not send this file")
                    raise RuntimeError(description)

    async def download_telegram_file(self, file_id: str, target: Path, max_size: int) -> int:
        info = await self.call("getFile", {"file_id": file_id})
        file_path = info.get("file_path")
        expected_size = info.get("file_size")
        if not file_path:
            raise RuntimeError("Telegram did not provide a file path")
        if expected_size is not None and int(expected_size) > max_size:
            raise DownloadError("That upload is larger than this bot's configured size limit.")
        local_path = Path(file_path)
        if local_path.is_absolute():
            if not local_path.is_file():
                raise RuntimeError("Local Bot API file path is not mounted into the bot container")
            local_size = local_path.stat().st_size
            if local_size > max_size:
                raise DownloadError("That upload is larger than this bot's configured size limit.")
            await asyncio.to_thread(shutil.copyfile, local_path, target)
            if expected_size is not None and local_size != int(expected_size):
                raise RuntimeError("Telegram file download was incomplete")
            return local_size
        url = f"{API_BASE}/file/bot{self.token}/{file_path.lstrip('/')}"
        written = 0
        async with self.session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError("Telegram could not provide the uploaded file")
            with target.open("wb") as output:
                async for block in response.content.iter_chunked(256 * 1024):
                    written += len(block)
                    if written > max_size:
                        raise DownloadError("That upload is larger than this bot's configured size limit.")
                    output.write(block)
        if expected_size is not None and written != int(expected_size):
            raise RuntimeError("Telegram file download was incomplete")
        return written


def _extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(URL_TRAILING_PUNCTUATION)


async def _handle_message(
    api: TelegramAPI,
    message: dict,
    semaphore: asyncio.Semaphore,
    active: set[int],
    store: FileStore,
) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user = message.get("from", {})
    user_id = user.get("id", chat_id)
    text = (message.get("text") or "").strip()
    if not chat_id:
        return
    if text.startswith("/start") or text.startswith("/help"):
        await api.send_text(
            chat_id,
            "Send me a file and I’ll make a share link that opens in any browser. You can also paste a public direct file link and I’ll send that file back here.\n\n"
            "Browser download links support byte ranges for compatible download managers. Speed depends on the server and network.\n"
            f"Current upload limit: {MAX_FILE_SIZE_MB} MB; URL download limit: {MAX_URL_DOWNLOAD_MB} MB. "
            f"Shared links expire after {LINK_TTL_HOURS} hours.",
        )
        return

    attachment = _get_attachment(message)
    if attachment:
        if not PUBLIC_BASE_URL:
            await api.send_text(chat_id, "The bot owner needs to configure PUBLIC_BASE_URL before upload links can be created.")
            return
        if user_id in active:
            await api.send_text(chat_id, "Your previous file is still being handled. Try again when it finishes.")
            return
        active.add(user_id)
        path: Path | None = None
        try:
            await api.send_text(chat_id, "Saving your file and creating a share link…")
            async with semaphore:
                path = store.new_path()
                size = await api.download_telegram_file(attachment["file_id"], path, MAX_FILE_SIZE_BYTES)
                token = store.register(
                    path=path,
                    filename=safe_filename(attachment["filename"]),
                    size=size,
                    content_type=attachment["content_type"],
                    owner_id=int(user_id),
                )
                path = None
            await api.send_text(
                chat_id,
                f"Your download link is ready:\n{PUBLIC_BASE_URL}/d/{token}\n\n"
                f"File size: {size / (1024 * 1024):.1f} MB · Expires in {LINK_TTL_HOURS} hours.",
            )
        except DownloadError as exc:
            await api.send_text(chat_id, str(exc))
        except Exception as exc:
            logger.warning("Upload handling failed (%s)", type(exc).__name__)
            await api.send_text(chat_id, "I couldn't create a download link for that file. Please try again.")
        finally:
            if path:
                path.unlink(missing_ok=True)
            active.discard(user_id)
        return

    url = _extract_url(text)
    if not url:
        await api.send_text(chat_id, "Paste a direct HTTP or HTTPS file link and I’ll fetch it for you.")
        return
    if user_id in active:
        await api.send_text(chat_id, "Your previous download is still running. Send another link after it finishes.")
        return

    active.add(user_id)
    try:
        await api.send_text(chat_id, "Downloading your file…")
        async with semaphore:
            path, filename, temporary = await download_file(
                url,
                max_size_bytes=MAX_URL_DOWNLOAD_BYTES,
                max_parallel_ranges=MAX_RANGES,
            )
            try:
                await api.send_file(chat_id, path, filename)
            finally:
                temporary.cleanup()
    except DownloadError as exc:
        await api.send_text(chat_id, str(exc))
    except Exception as exc:
        logger.warning("Request failed (%s)", type(exc).__name__)
        await api.send_text(
            chat_id,
            "The download finished or started, but Telegram could not send the file. It may exceed Telegram’s upload limit.",
        )
    finally:
        active.discard(user_id)


def _get_attachment(message: dict) -> dict | None:
    for field, content_type, default_name in (
        ("document", "application/octet-stream", "file"),
        ("video", "video/mp4", "video"),
        ("audio", "audio/mpeg", "audio"),
        ("animation", "video/mp4", "animation"),
        ("voice", "audio/ogg", "voice"),
        ("video_note", "video/mp4", "video-note"),
    ):
        item = message.get(field)
        if item:
            return {
                "file_id": item["file_id"],
                "filename": item.get("file_name") or default_name,
                "content_type": item.get("mime_type") or content_type,
            }
    photos = message.get("photo")
    if photos:
        largest = photos[-1]
        return {"file_id": largest["file_id"], "filename": "photo.jpg", "content_type": "image/jpeg"}
    return None


async def run() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment or .env file before starting the bot.")
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=90)
    semaphore = asyncio.Semaphore(MAX_ACTIVE)
    active: set[int] = set()
    store = FileStore(DATA_DIR, ttl_hours=LINK_TTL_HOURS)
    store.cleanup_expired()
    offset = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = TelegramAPI(session, TOKEN)
        logger.info(
            "LinkDrop bot started; upload limit %s MB, URL download limit %s MB",
            MAX_FILE_SIZE_MB,
            MAX_URL_DOWNLOAD_MB,
        )
        runner = web.AppRunner(create_web_app(store), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
        await site.start()
        logger.info("Download server listening on port %s", PORT)
        try:
            while True:
                try:
                    updates = await api.call(
                        "getUpdates",
                        {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                    )
                    for update in updates:
                        offset = max(offset, int(update["update_id"]) + 1)
                        message = update.get("message")
                        if message:
                            asyncio.create_task(_handle_message(api, message, semaphore, active, store))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Polling error (%s); retrying shortly", type(exc).__name__)
                    await asyncio.sleep(3)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
