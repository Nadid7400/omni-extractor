# Omni Extractor — media downloader

A small FastAPI + yt-dlp web app for inspecting and downloading **publicly accessible** media. It supports video quality selection, MP3/M4A/FLAC/WAV audio output, browser-local recent links, batch metadata extraction, optional account linking, Docker, and Render.

## Cookies: none by default

Out of the box there is no cookie upload, cookie file, cookie environment variable, proxy configuration, sign-in session, or browser credential handling. It makes ordinary unauthenticated yt-dlp requests. That means public links work where the source platform permits them, but **no downloader can promise every platform/video will work without authorization**. YouTube and other services may block datacenter requests, require sign-in, or apply bot verification; this app will report the error rather than bypass those checks. Private, age-restricted, or otherwise gated media is not supported.

The one exception is deliberate, opt-in, and user-owned: **account linking** (below) lets a visitor supply their own YouTube/Google cookies so the server makes requests as that visitor's signed-in session. Nothing is enabled unless the user links their own account, and it is dropped when they unlink or after the session expires.

YouTube extraction (and some other sites) uses a JavaScript runtime; the Docker image bundles Deno for this. Optionally, `YTDLP_EXTRACTOR_ARGS` can tune yt-dlp extractor behavior on a deployment, e.g. `youtube:player_client=android_vr,tv,ios` to try different YouTube clients. On hosted deployments most YouTube links additionally need account linking, because YouTube challenges datacenter IPs.

## Optional account linking

A visitor can link **their own** YouTube/Google account by pasting a Netscape `cookies.txt` export (e.g. from the "Get cookies.txt LOCALLY" browser extension). The server then passes those cookies to yt-dlp, so downloads run as that visitor's signed-in session.

- `GET /api/account` — `{"linked": false}` or `{"linked": true, "entries": <n>, "expires_in": <seconds>}`.
- `POST /api/account/link` with `{"cookies": "<cookies.txt contents>"}` — keeps only youtube.com/google.com cookie lines, stores them in memory, and sets an HttpOnly session cookie for the browser.
- `POST /api/account/unlink` — forgets the cookies for that session.

Cookies are held in server memory keyed by the session cookie for `ACCOUNT_TTL_SECONDS` (default 2 hours, refreshed on use), written to a short-lived temp file only while a download runs, and never written to persistent storage. There is no Google OAuth flow: yt-dlp has no download API for one, so the cookie export is the only mechanism it can use. The user is responsible for the cookies they import — a Google session cookie grants account access, so it should only be pasted into a deployment they trust.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open `http://localhost:8000`. To choose another port, run `python run.py --port 8080`. For LAN use, `python run.py --host 0.0.0.0 --no-browser` and visit the machine's LAN address.

FFmpeg is used for MP4 merging and audio conversion. The app detects system FFmpeg automatically and otherwise uses the `imageio-ffmpeg` binary installed by requirements.

## Docker

```bash
docker build -t omni-extractor .
docker run --rm -p 8000:8000 omni-extractor
```

A Render blueprint is included in `render.yaml`. Free-tier or datacenter IP restrictions at media platforms still apply. On small instances heavy downloads take minutes and run one at a time; the web UI uses the job endpoints so a long-running download never holds an HTTP request open.

## API

- `GET /api/info?url=...` or `POST /api/info` with `{"url":"..."}` — title, creator, thumbnail, duration, video formats, and audio choices.
- `GET /api/download?url=...&type=video&quality=1080&format=mp4` — video (quality may be `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`, `240`, or `144`).
- `GET /api/download?url=...&type=audio&format=mp3&bitrate=320` — MP3, M4A, FLAC, or WAV.
- `POST /api/download-job?url=...&type=video&quality=1080&format=mp4` — starts a background download job (same parameters as `/api/download`) and returns `{"job_id":"..."}`. The web UI uses this so long downloads never leave an HTTP request idle (which platform proxies terminate). Poll `GET /api/download-job/{job_id}` for `status` (`queued` → `working` → `done`/`error`) plus progress bytes, then save `GET /api/download-job/{job_id}/file`. Prepared files are deleted after delivery and jobs expire after 30 minutes. Jobs run one at a time by default (set `MAX_CONCURRENT_JOBS` to change); the queue rejects new jobs with HTTP 429 beyond `MAX_QUEUED_JOBS` (default 20).
- `POST /api/extract-batch` with `{"text":"... URLs ..."}` — metadata for up to ten links.
- `GET /api/account`, `POST /api/account/link`, `POST /api/account/unlink` — optional account linking (see above).
- `GET /api/network-info` and `/api/health`.

Downloads are written to temporary directories and removed after the response finishes. The browser keeps recent links in its own local storage. No app account is required; account linking is optional, per browser session, and only used to authenticate the user's own downloads. Basic public-IP URL validation is applied before extraction.

## Responsible use

Use only for media you own or are permitted to save, and follow platform terms and applicable law. The software does not remove DRM or evade access controls.

## License

MIT
