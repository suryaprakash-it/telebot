# LinkDrop Telegram Bot

LinkDrop turns Telegram uploads into shareable browser download links. Send the bot a document and it replies with a link that works in Chrome, Firefox, Edge, Safari, and other browsers. The web endpoint supports HTTP byte ranges so compatible browsers and download managers can request different parts in parallel.

You can also paste a public file URL into the bot and it will download and send the file back in Telegram. For URL downloads it uses parallel HTTP range requests when the host supports them, with a single-connection fallback.

Parallel connections can improve speed on some hosts, but no bot can guarantee 10× speed. The host, server bandwidth, internet connection, and browser/download manager all affect transfer speed. For fast shared links, deploy behind a well-connected server or CDN.

## Run it

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Copy `.env.example` to `.env`, set `TELEGRAM_BOT_TOKEN`, and set `PUBLIC_BASE_URL` to the HTTPS domain that will serve downloads.
3. Start the bot with Docker Compose:

   ```sh
   docker compose up --build
   ```

   Or run it with Python 3.11 or newer:

   ```sh
   python -m venv .venv
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.bot
   ```

4. Point your HTTPS reverse proxy for `PUBLIC_BASE_URL` to port `8080` on this service. The included container serves plain HTTP; put TLS at the proxy.
5. Open your bot in Telegram and send `/start`. Send a document to get a link, or paste a direct file URL to fetch it into Telegram.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | required | Token from BotFather |
| `MAX_FILE_SIZE_MB` | `20` | Maximum Telegram upload the bot will turn into a link |
| `MAX_URL_DOWNLOAD_MB` | `49` | Maximum public URL download the bot will send back through Telegram |
| `MAX_PARALLEL_RANGES` | `8` | Maximum parallel pieces for a single download |
| `MAX_ACTIVE_DOWNLOADS` | `2` | Maximum simultaneous users being served |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Telegram Bot API base URL; change only when using a compatible self-hosted Bot API server |
| `PUBLIC_BASE_URL` | required for upload links | Public HTTPS base URL used in generated links |
| `DATA_DIR` | `./data` | Persistent file and SQLite storage directory |
| `LINK_TTL_HOURS` | `168` | How long generated links remain valid |
| `PORT` | `8080` | HTTP download server port |

Links must point to publicly reachable HTTP or HTTPS resources. Websites that require a login, browser cookies, JavaScript interaction, or DRM are not supported; use a direct download link. The bot blocks private and local network destinations and checks redirects too.

Telegram's hosted Bot API currently limits bot file downloads to 20 MB. You can raise `MAX_FILE_SIZE_MB` when using a compatible local Bot API server. If that server returns local file paths, mount its file directory into this bot container at the same path. See [Telegram's Bot API file limits](https://core.telegram.org/bots/api#getfile).

Uploaded files are kept in the configured persistent data directory until their links expire. Expired files are removed when the bot starts and as new links are created. Files downloaded from URLs are stored temporarily and removed after the bot sends them.

## Project layout

- `app/bot.py` — Telegram long polling and message handling
- `app/downloader.py` — safe URL handling and parallel/fallback downloads
- `app/file_store.py` — expiring share links and persistent file metadata
- `app/web.py` — browser download endpoint with HTTP range support
- `Dockerfile` and `compose.yaml` — containerized runtime
