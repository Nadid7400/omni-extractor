# Omni Extractor — cookie-free media downloader

A small FastAPI + yt-dlp web app for inspecting and downloading **publicly accessible** media. It supports video quality selection, MP3/M4A/FLAC/WAV audio output, browser-local recent links, batch metadata extraction, Docker, and Render.

## No cookies required

There is no cookie upload, cookie file, cookie environment variable, proxy configuration, sign-in session, or browser credential handling in this app. It makes ordinary unauthenticated yt-dlp requests. That means public links work where the source platform permits them, but **no downloader can promise every platform/video will work without authorization**. YouTube and other services may block datacenter requests, require sign-in, or apply bot verification; this app will report the error rather than bypass those checks. Private, age-restricted, or otherwise gated media is not supported.

YouTube extraction (and some other sites) uses a JavaScript runtime; the Docker image bundles Deno for this. Optionally, `YTDLP_EXTRACTOR_ARGS` can tune yt-dlp extractor behavior on a deployment, e.g. `youtube:player_client=android_vr,tv,ios` to try different YouTube clients.

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
- `GET /api/network-info` and `/api/health`.

Downloads are written to temporary directories and removed after the response finishes. The browser keeps recent links in its own local storage; no app account is used. Basic public-IP URL validation is applied before extraction.

## Responsible use

Use only for media you own or are permitted to save, and follow platform terms and applicable law. The software does not remove DRM or evade access controls.

## License

MIT
