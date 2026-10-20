from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from starlette.concurrency import run_in_threadpool

APP_DIR = Path(__file__).parent
app = FastAPI(title="Omni Extractor", version="1.0.0", description="Cookie-free public media extractor")

# Prefer a system FFmpeg installation; use imageio-ffmpeg's bundled binary if present.
FFMPEG_LOCATION: str | None = shutil.which("ffmpeg")
if not FFMPEG_LOCATION:
    try:
        import imageio_ffmpeg
        FFMPEG_LOCATION = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_LOCATION = None

# Job-based downloads: the server prepares the file in the background while the
# browser polls. This keeps individual HTTP requests short, so long downloads do
# not sit idle on platform proxies (which terminate them) and heavy work is
# serialized to protect small instances.
DOWNLOAD_JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 30 * 60
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
MAX_QUEUED_JOBS = max(1, int(os.getenv("MAX_QUEUED_JOBS", "20")))
_JOB_SLOTS = threading.Semaphore(MAX_CONCURRENT_JOBS)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Optional quality cap for resource-constrained hosts (e.g. Render free tier).
# Set MAX_VIDEO_HEIGHT=720 to prevent users from attempting 1080p/4K downloads
# that will never complete within the available RAM/disk budget.
MAX_VIDEO_HEIGHT = int(os.getenv("MAX_VIDEO_HEIGHT", "0"))  # 0 = no cap

# Optional account linking. Users may supply their own YouTube/Google cookies so
# yt-dlp downloads run as their signed-in session (YouTube is unusable from
# datacenter IPs without one). Cookies are held in memory only, keyed by a
# session cookie, and are written to a short-lived temp file per job that is
# removed as soon as the job ends.
LINKED_ACCOUNTS: dict[str, dict[str, Any]] = {}
ACCOUNT_LOCK = threading.Lock()
ACCOUNT_TTL_SECONDS = 2 * 60 * 60
MAX_LINKED_ACCOUNTS = 1000
MAX_COOKIE_BYTES = 256 * 1024
COOKIE_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "googlevideo.com",
    "ytimg.com",
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    "google.co",
)
SESSION_COOKIE_NAME = "omni_session"
SERVER_COOKIES_ENV = os.getenv("SERVER_COOKIES") or os.getenv("YTDLP_COOKIES") or ""
SERVER_COOKIES_FILE = APP_DIR / "cookies.txt"

# Google Sign-In (OpenID Connect authorization-code flow). This authenticates the
# visitor's Google identity only: it returns an id_token for profile/email, and
# deliberately never requests YouTube scopes, because no Google OAuth scope can
# authorize media downloads (yt-dlp needs the account's browser session, which
# account linking above provides). Disabled unless credentials are configured.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
OAUTH_STATE_COOKIE = "omni_oauth_state"


def _job_sweep() -> None:
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, job in DOWNLOAD_JOBS.items() if now - job["updated"] > JOB_TTL_SECONDS]
        for jid in stale:
            job = DOWNLOAD_JOBS.pop(jid)
            if job.get("tempdir"):
                shutil.rmtree(job["tempdir"], ignore_errors=True)


def _update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if job:
            job.update(fields)
            job["updated"] = time.time()


def _progress_hook(job_id: str) -> Callable[[dict[str, Any]], None]:
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            _update_job(
                job_id,
                stage="downloading",
                downloaded=data.get("downloaded_bytes"),
                total=data.get("total_bytes") or data.get("total_bytes_estimate"),
            )
        elif status == "finished":
            _update_job(job_id, stage="processing", downloaded=data.get("total_bytes"), total=data.get("total_bytes"))
    return hook


def _account_sweep() -> None:
    now = time.time()
    with ACCOUNT_LOCK:
        stale = [token for token, acct in LINKED_ACCOUNTS.items() if now - acct["updated"] > ACCOUNT_TTL_SECONDS]
        for token in stale:
            LINKED_ACCOUNTS.pop(token, None)


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = time.time()
    with ACCOUNT_LOCK:
        acct = LINKED_ACCOUNTS.get(token)
        if not acct:
            return None
        if now - acct["updated"] > ACCOUNT_TTL_SECONDS:
            LINKED_ACCOUNTS.pop(token, None)
            return None
        acct["updated"] = now
        return dict(acct)


def update_session(token: str, **fields: Any) -> None:
    now = time.time()
    with ACCOUNT_LOCK:
        acct = LINKED_ACCOUNTS.get(token)
        if acct is None:
            acct = {"cookies": None, "entries": 0, "identity": None}
            LINKED_ACCOUNTS[token] = acct
        acct.update(fields)
        acct["updated"] = now


def account_cookie_blob(token: str | None) -> str | None:
    session = get_session(token)
    return session.get("cookies") if session else None


def google_auth_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def public_base_url(http_request: Request) -> str:
    override = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
    if override:
        return override
    host = http_request.headers.get("host") or http_request.url.netloc
    scheme = "https" if request_is_https(http_request) else "http"
    return f"{scheme}://{host}"


def google_authorize_url(redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
            "access_type": "online",
        }
    )
    return f"{GOOGLE_AUTH_ENDPOINT}?{query}"


def exchange_google_code(code: str, redirect_uri: str) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_google_id_token(id_token: str) -> dict[str, Any]:
    """Decode and sanity-check the id_token Google returned to our token endpoint over TLS."""
    try:
        payload_segment = id_token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        raise ValueError("Could not read the Google response.")
    if claims.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("That Google token was issued for a different application.")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("Unexpected Google token issuer.")
    if float(claims.get("exp", 0)) < time.time():
        raise ValueError("The Google sign-in had already expired.")
    return claims


@contextlib.contextmanager
def linked_cookiefile(token: str | None):
    """Yield a temporary cookiefile for the linked session, or fallback to server cookies."""
    blob = account_cookie_blob(token)
    if not blob:
        blob, _ = get_server_cookie_blob()
    if not blob:
        yield None
        return
    handle_fd, path = tempfile.mkstemp(prefix="omni-cookies-", suffix=".txt")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(blob)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def request_is_https(http_request: Request) -> bool:
    """Honor X-Forwarded-Proto (Render/most proxies) so cookie flags match the real scheme."""
    forwarded = http_request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip().lower() or http_request.url.scheme
    return scheme == "https"


def validate_cookie_blob(raw: str) -> tuple[str, int]:
    """Keep only youtube/google cookie lines from an exported Netscape cookies.txt."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip().lstrip("\ufeff")
    if not text:
        raise HTTPException(status_code=400, detail="Paste the contents of a cookies.txt file exported for youtube.com.")
    if len(text) > MAX_COOKIE_BYTES:
        raise HTTPException(status_code=400, detail="That cookies file is too large.")
    kept: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        probe = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        if probe.startswith("#"):
            continue
        parts = probe.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lstrip(".").lower()
        if not any(domain == allowed or domain.endswith("." + allowed) for allowed in COOKIE_DOMAINS):
            continue
        kept.append(line)
    if not kept:
        raise HTTPException(status_code=400, detail="No youtube.com or google.com cookies were found in that file.")
    blob = "# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n"
    return blob, len(kept)


def get_server_cookie_blob() -> tuple[str | None, int]:
    """Retrieve server-wide cookie blob if configured via env var or cookies.txt."""
    raw = SERVER_COOKIES_ENV.strip()
    if raw:
        if not raw.startswith("#") and "\t" not in raw:
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except Exception:
                pass
        try:
            return validate_cookie_blob(raw)
        except Exception:
            return raw, 1
    if SERVER_COOKIES_FILE.is_file():
        try:
            text = SERVER_COOKIES_FILE.read_text(encoding="utf-8")
            return validate_cookie_blob(text)
        except Exception:
            pass
    return None, 0


def validate_public_url(raw_url: str) -> str:
    """Basic SSRF guard. Only public HTTP(S) URLs are accepted."""
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
            raise ValueError
        try:
            addresses = {ipaddress.ip_address(hostname)}
        except ValueError:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, None)}
        if not addresses or any(not addr.is_global for addr in addresses):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Enter a valid public http:// or https:// media URL.")
    return raw_url.strip()


def parse_extractor_args(raw: str) -> dict[str, dict[str, list[str]]]:
    """Parse 'extractor:key=value,...;extractor2:key2=v1,v2' into yt-dlp extractor_args."""
    parsed: dict[str, dict[str, list[str]]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk or "=" not in chunk:
            continue
        extractor, rest = chunk.split(":", 1)
        key, value = rest.split("=", 1)
        values = [item.strip() for item in value.split(",") if item.strip()]
        if extractor.strip() and key.strip() and values:
            parsed.setdefault(extractor.strip(), {})[key.strip()] = values
    return parsed


def ydl_options(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
        "cachedir": False,
    }
    raw_args = os.getenv("YTDLP_EXTRACTOR_ARGS", "")
    extractor_args = parse_extractor_args(raw_args)
    if extractor_args:
        opts["extractor_args"] = extractor_args
    elif extra and extra.get("cookiefile"):
        opts["extractor_args"] = {"youtube": {"player_client": ["web_embedded", "tv_downgraded", "web", "visionos"]}}
    else:
        # Default fallback for datacenter IPs (Render, AWS, GCP) where YouTube blocks web client with bot challenge
        opts["extractor_args"] = {"youtube": {"player_client": ["visionos", "android"]}}
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if extra:
        opts.update(extra)
    return opts


def clean_metadata(info: dict[str, Any]) -> dict[str, Any]:
    formats: list[dict[str, Any]] = []
    for item in info.get("formats") or []:
        height = item.get("height")
        if not height or item.get("vcodec") == "none":
            continue
        height = int(height)
        if MAX_VIDEO_HEIGHT and height > MAX_VIDEO_HEIGHT:
            continue
        # One representative format per height; prefer H.264 (avc1) for maximum device compatibility
        existing = next((f for f in formats if f["height"] == height), None)
        size = item.get("filesize") or item.get("filesize_approx")
        candidate = {
            "format_id": str(item.get("format_id", "")),
            "height": height,
            "label": f"{height}p",
            "ext": item.get("ext") or "mp4",
            "filesize": size,
            "fps": item.get("fps"),
            "vcodec": item.get("vcodec"),
        }
        is_avc = str(candidate.get("vcodec", "")).startswith("avc1")
        existing_is_avc = str(existing.get("vcodec", "")).startswith("avc1") if existing else False
        if existing is None:
            formats.append(candidate)
        elif is_avc and not existing_is_avc:
            formats[formats.index(existing)] = candidate
        elif (not existing_is_avc or is_avc) and size and (not existing.get("filesize") or size > existing["filesize"]):
            formats[formats.index(existing)] = candidate
    formats.sort(key=lambda f: f["height"], reverse=True)
    return {
        "title": info.get("title") or "Untitled media",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown creator",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "platform": info.get("extractor_key") or info.get("extractor") or "Media",
        "video_downloads": formats,
        "audio_downloads": [
            {"format": "mp3", "label": "MP3 · 320 kbps", "bitrate": 320},
            {"format": "mp3", "label": "MP3 · 256 kbps", "bitrate": 256},
            {"format": "mp3", "label": "MP3 · 192 kbps", "bitrate": 192},
            {"format": "mp3", "label": "MP3 · 128 kbps", "bitrate": 128},
            {"format": "m4a", "label": "M4A · AAC", "bitrate": 0},
            {"format": "flac", "label": "FLAC · lossless", "bitrate": 0},
            {"format": "wav", "label": "WAV · PCM", "bitrate": 0},
        ],
    }


def extract_info(raw_url: str, cookiefile: str | None = None) -> dict[str, Any]:
    extra = {"cookiefile": cookiefile} if cookiefile else None
    with yt_dlp.YoutubeDL(ydl_options(extra)) as ydl:
        info = ydl.extract_info(raw_url, download=False)
        if not info:
            raise RuntimeError("No media information was returned.")
        return clean_metadata(info)


def search_youtube(query: str, limit: int = 10, cookiefile: str | None = None) -> list[dict[str, Any]]:
    """Search YouTube videos and return metadata without downloading."""
    clean_query = query.strip()
    if not clean_query:
        return []
    search_term = f"ytsearch{limit}:{clean_query}"
    extra: dict[str, Any] = {"extract_flat": True, "quiet": True, "no_warnings": True}
    if cookiefile:
        extra["cookiefile"] = cookiefile
    with yt_dlp.YoutubeDL(ydl_options(extra)) as ydl:
        res = ydl.extract_info(search_term, download=False)
        if not res:
            return []
        entries = res.get("entries") or []
        results: list[dict[str, Any]] = []
        for e in entries:
            if not e:
                continue
            video_id = e.get("id")
            url = e.get("url")
            if video_id and (not url or not str(url).startswith("http")):
                url = f"https://www.youtube.com/watch?v={video_id}"
            thumbs = e.get("thumbnails") or []
            thumb_url = None
            if thumbs:
                thumb_url = thumbs[-1].get("url")
            if not thumb_url and video_id:
                thumb_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
            results.append({
                "id": video_id,
                "url": url,
                "title": e.get("title") or "Untitled",
                "uploader": e.get("uploader") or e.get("channel") or "YouTube",
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
                "thumbnail": thumb_url,
            })
        return results


class InfoRequest(BaseModel):
    url: str


class BatchRequest(BaseModel):
    text: str


class LinkCookiesRequest(BaseModel):
    cookies: str


async def _keep_alive_loop() -> None:
    """Periodic self-ping to keep cloud instances (Render, Fly, etc.) permanently awake."""
    target_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("KEEP_ALIVE_URL")
        or ("https://omni-extractor.onrender.com" if (os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID")) else "")
    )
    if not target_url:
        return
    ping_url = f"{target_url.rstrip('/')}/api/health"
    await asyncio.sleep(60)  # Initial wait for server bind
    while True:
        try:
            req = urllib.request.Request(
                ping_url,
                headers={"User-Agent": "OmniExtractor-KeepAlive/1.0"},
            )
            await run_in_threadpool(lambda: urllib.request.urlopen(req, timeout=15).read())
        except Exception:
            pass
        await asyncio.sleep(480)  # 8 minutes interval (Render free tier idle timer is 15 minutes)


@app.on_event("startup")
async def _startup_event() -> None:
    task = asyncio.create_task(_keep_alive_loop())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "ffmpeg": bool(FFMPEG_LOCATION)}


@app.get("/api/info")
async def info_get(http_request: Request, url: str = Query(..., min_length=8, max_length=2048)) -> dict[str, Any]:
    safe_url = validate_public_url(url)
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    try:
        with linked_cookiefile(token) as cookiefile:
            return await run_in_threadpool(extract_info, safe_url, cookiefile)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read this media URL: {str(exc)[:240]}")


@app.post("/api/info")
async def info_post(http_request: Request, payload: InfoRequest) -> dict[str, Any]:
    return await info_get(http_request, payload.url)


@app.get("/api/search")
async def search_endpoint(
    http_request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=25),
) -> dict[str, Any]:
    """Search YouTube videos and return metadata options."""
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    try:
        with linked_cookiefile(token) as cookiefile:
            items = await run_in_threadpool(search_youtube, q, limit, cookiefile)
            return {"query": q, "results": items, "count": len(items)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"YouTube search failed: {str(exc)[:200]}")


@app.get("/api/account")
async def account_info(http_request: Request) -> dict[str, Any]:
    _account_sweep()
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(token)
    server_blob, server_entries = get_server_cookie_blob()
    user_cookies = bool(session and session.get("cookies"))
    linked = user_cookies or bool(server_blob)
    entries = (session.get("entries") if user_cookies else server_entries) or 0
    return {
        "linked": linked,
        "server_managed": bool(server_blob and not user_cookies),
        "entries": entries,
        "identity": session.get("identity") if session else None,
        "expires_in": ACCOUNT_TTL_SECONDS,
        "google": google_auth_configured(),
    }


@app.post("/api/account/link")
async def account_link(http_request: Request, payload: LinkCookiesRequest, response: Response) -> dict[str, Any]:
    blob, entries = validate_cookie_blob(payload.cookies)
    token = http_request.cookies.get(SESSION_COOKIE_NAME) or uuid.uuid4().hex
    _account_sweep()
    with ACCOUNT_LOCK:
        if token not in LINKED_ACCOUNTS and len(LINKED_ACCOUNTS) >= MAX_LINKED_ACCOUNTS:
            oldest = min(LINKED_ACCOUNTS, key=lambda key: LINKED_ACCOUNTS[key]["updated"])
            LINKED_ACCOUNTS.pop(oldest, None)
    update_session(token, cookies=blob, entries=entries)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=ACCOUNT_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_https(http_request),
        path="/",
    )
    return {"linked": True, "entries": entries, "expires_in": ACCOUNT_TTL_SECONDS}


@app.post("/api/account/unlink")
async def account_unlink(http_request: Request, response: Response) -> dict[str, Any]:
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with ACCOUNT_LOCK:
            LINKED_ACCOUNTS.pop(token, None)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=request_is_https(http_request), httponly=True, samesite="lax")
    return {"linked": False}


@app.get("/api/auth/config")
async def auth_config() -> dict[str, Any]:
    return {"google": google_auth_configured()}


@app.get("/api/auth/google/login")
async def google_login(http_request: Request) -> Response:
    if not google_auth_configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this deployment.")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(
        google_authorize_url(f"{public_base_url(http_request)}/api/auth/google/callback", state),
        status_code=307,
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request_is_https(http_request),
        path="/",
    )
    return response


@app.get("/api/auth/google/callback")
async def google_callback(
    http_request: Request,
    code: str = Query("", max_length=4096),
    state: str = Query("", max_length=256),
    error: str = Query("", max_length=120),
) -> Response:
    if error:
        return RedirectResponse("/?auth_error=denied", status_code=303)
    expected_state = http_request.cookies.get(OAUTH_STATE_COOKIE)
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return RedirectResponse("/?auth_error=state", status_code=303)
    if not google_auth_configured():
        return RedirectResponse("/?auth_error=disabled", status_code=303)
    try:
        tokens = await run_in_threadpool(
            exchange_google_code, code, f"{public_base_url(http_request)}/api/auth/google/callback"
        )
        claims = parse_google_id_token(tokens.get("id_token", ""))
    except Exception:
        return RedirectResponse("/?auth_error=google", status_code=303)

    session_token = http_request.cookies.get(SESSION_COOKIE_NAME) or uuid.uuid4().hex
    update_session(
        session_token,
        identity={
            "email": claims.get("email"),
            "name": claims.get("name") or claims.get("email"),
            "picture": claims.get("picture"),
            "email_verified": bool(claims.get("email_verified")),
        },
    )
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=ACCOUNT_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_https(http_request),
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(http_request: Request, response: Response) -> dict[str, Any]:
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with ACCOUNT_LOCK:
            LINKED_ACCOUNTS.pop(token, None)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=request_is_https(http_request), httponly=True, samesite="lax")
    return {"linked": False, "identity": None}


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value).strip(" .")
    return (value[:150] or "media")


def find_downloaded_file(folder: str) -> Path:
    files = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("The media file was not created.")
    return max(files, key=lambda p: p.stat().st_size)


def validate_download_args(type: str, quality: str, format: str) -> None:
    if type == "audio" and format == "mp4":
        raise HTTPException(status_code=400, detail="Choose an audio format.")
    if type == "video" and format != "mp4":
        raise HTTPException(status_code=400, detail="Choose MP4 for video downloads.")
    if type == "video" and quality != "best":
        try:
            if int(quality) not in {144, 240, 360, 480, 720, 1080, 1440, 2160}:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Unsupported video quality.")


def build_download_options(
    tempdir: str,
    type: str,
    quality: str,
    format: str,
    bitrate: int,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    cookiefile: str | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "outtmpl": os.path.join(tempdir, "%(title).150B.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
    }
    if cookiefile:
        options["cookiefile"] = cookiefile
    if type == "video":
        # Apply quality cap if set (e.g. MAX_VIDEO_HEIGHT=720 on Render free tier).
        if MAX_VIDEO_HEIGHT and quality == "best":
            quality = str(MAX_VIDEO_HEIGHT)
        if quality == "best":
            # Prefer pre-merged single-file formats first to avoid 3× disk usage
            # from downloading separate video + audio streams then FFmpeg-merging.
            options["format"] = "best/bestvideo+bestaudio"
        else:
            max_height = int(quality)
            options["format"] = f"best[height<={max_height}]/bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]"
        options["merge_output_format"] = "mp4"
    else:
        options["format"] = "bestaudio/best"
        if format == "mp3":
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)}]
        elif format in {"m4a", "flac", "wav"}:
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": format}]
    if progress_hook:
        options["progress_hooks"] = [progress_hook]
    return options


def finish_download(tempdir: str, type: str, format: str, title: str | None) -> tuple[Path, int]:
    path = find_downloaded_file(tempdir)
    if title:
        suffix = ".mp4" if type == "video" else f".{format}"
        path = path.rename(path.with_name(safe_filename(title) + suffix))
    return path, path.stat().st_size


def download_error_message(exc: Exception, type: str) -> str:
    message = str(exc)
    if not FFMPEG_LOCATION and ("ffmpeg" in message.lower() or type == "audio"):
        message = "FFmpeg is required for this conversion. Install ffmpeg or imageio-ffmpeg."
    return message[:240]


@app.get("/api/download")
async def download(
    http_request: Request,
    url: str = Query(..., min_length=8, max_length=2048),
    type: str = Query("video", pattern="^(video|audio)$"),
    quality: str = Query("best", max_length=8),
    format: str = Query("mp3", pattern="^(mp3|m4a|flac|wav|mp4)$"),
    bitrate: int = Query(320, ge=64, le=320),
    title: str | None = Query(None, max_length=180),
):
    safe_url = validate_public_url(url)
    validate_download_args(type, quality, format)

    tempdir = tempfile.mkdtemp(prefix="omni-")

    try:
        with linked_cookiefile(http_request.cookies.get(SESSION_COOKIE_NAME)) as cookiefile:
            options = build_download_options(tempdir, type, quality, format, bitrate, cookiefile=cookiefile)
            await run_in_threadpool(lambda: _download_sync(safe_url, options))
        path, _ = finish_download(tempdir, type, format, title)
        return FileResponse(
            path,
            filename=path.name,
            media_type="video/mp4" if type == "video" else "application/octet-stream",
            headers={"Cache-Control": "no-store"},
            # Remove the temporary download directory after the response finishes streaming.
            background=BackgroundTask(shutil.rmtree, tempdir, True),
        )
    except HTTPException:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Download failed: {download_error_message(exc, type)}")


def _run_download_job(
    job_id: str,
    safe_url: str,
    type: str,
    quality: str,
    format: str,
    bitrate: int,
    title: str | None,
    tempdir: str,
    session_token: str | None,
) -> None:
    with _JOB_SLOTS:
        _update_job(job_id, status="working", stage="downloading")
        try:
            with linked_cookiefile(session_token) as cookiefile:
                options = build_download_options(tempdir, type, quality, format, bitrate, _progress_hook(job_id), cookiefile)
                _download_sync(safe_url, options)
            path, size = finish_download(tempdir, type, format, title)
            _update_job(job_id, status="done", stage="done", path=str(path), filename=path.name, size=size, tempdir=tempdir)
        except Exception as exc:
            shutil.rmtree(tempdir, ignore_errors=True)
            _update_job(job_id, status="error", path=None, error=download_error_message(exc, type), tempdir=None)


JOB_PUBLIC_FIELDS = ("status", "stage", "error", "filename", "size", "downloaded", "total")


@app.post("/api/download-job")
async def download_job(
    http_request: Request,
    url: str = Query(..., min_length=8, max_length=2048),
    type: str = Query("video", pattern="^(video|audio)$"),
    quality: str = Query("best", max_length=8),
    format: str = Query("mp3", pattern="^(mp3|m4a|flac|wav|mp4)$"),
    bitrate: int = Query(320, ge=64, le=320),
    title: str | None = Query(None, max_length=180),
) -> dict[str, Any]:
    safe_url = validate_public_url(url)
    validate_download_args(type, quality, format)
    _job_sweep()
    session_token = http_request.cookies.get(SESSION_COOKIE_NAME)

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        queued_ahead = sum(1 for job in DOWNLOAD_JOBS.values() if job["status"] in {"queued", "working"})
        if queued_ahead >= MAX_QUEUED_JOBS:
            raise HTTPException(status_code=429, detail="The download queue is full — try again in a minute.")
        tempdir = tempfile.mkdtemp(prefix="omni-")
        DOWNLOAD_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "stage": None,
            "error": None,
            "filename": None,
            "size": None,
            "downloaded": None,
            "total": None,
            "type": type,
            "path": None,
            "tempdir": tempdir,
            "created": time.time(),
            "updated": time.time(),
        }
    task = asyncio.create_task(
        run_in_threadpool(_run_download_job, job_id, safe_url, type, quality, format, bitrate, title, tempdir, session_token)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"job_id": job_id, "queued_ahead": queued_ahead}


@app.get("/api/download-job/{job_id}")
async def download_job_status(job_id: str) -> dict[str, Any]:
    _job_sweep()
    with JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="This download was not found or has expired.")
        return {field: job.get(field) for field in JOB_PUBLIC_FIELDS}


def resolve_job_file(job_id: str) -> tuple[Path, str, str | None, str]:
    with JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="This download was not found or has expired.")
        if job["status"] != "done" or not job.get("path"):
            raise HTTPException(status_code=409, detail="This download is not ready yet.")
        path = Path(job["path"])
        filename = job.get("filename") or path.name
        tempdir = job.get("tempdir")
        media_type = "video/mp4" if job.get("type") == "video" else "application/octet-stream"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The prepared file is no longer available.")
    return path, filename, tempdir, media_type


@app.get("/api/download-job/{job_id}/file")
async def download_job_file(job_id: str) -> Response:
    path, filename, tempdir, media_type = resolve_job_file(job_id)
    file_size = path.stat().st_size

    def _cleanup() -> None:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        with JOBS_LOCK:
            DOWNLOAD_JOBS.pop(job_id, None)

    # Stream in 256 KB chunks so bytes keep flowing and Render's proxy
    # (which kills connections idle for ~100 s) does not terminate the transfer.
    async def _stream():
        try:
            with open(path, "rb") as f:
                while chunk := f.read(256 * 1024):
                    yield chunk
        finally:
            _cleanup()

    encoded = urllib.parse.quote(filename, safe="")
    return StreamingResponse(
        _stream(),
        media_type=media_type,
        headers={
            "Content-Length": str(file_size),
            "Content-Disposition": f'attachment; filename="{encoded}"; filename*=UTF-8\'\'{encoded}',
            "Cache-Control": "no-store",
        },
    )


@app.head("/api/download-job/{job_id}/file")
async def download_job_file_head(job_id: str) -> FileResponse:
    # HEAD probes (download managers, scanners) get headers only and must not
    # consume the job, so this response carries no cleanup background task.
    path, filename, _, media_type = resolve_job_file(job_id)
    return FileResponse(path, filename=filename, media_type=media_type, headers={"Cache-Control": "no-store"})


def _download_sync(url: str, options: dict[str, Any]) -> None:
    with yt_dlp.YoutubeDL(ydl_options(options)) as ydl:
        ydl.download([url])


def make_content_disposition(name: str, ext: str) -> str:
    """Generate RFC 6266 compliant Content-Disposition with clean ASCII fallback for Android/iOS."""
    ascii_clean = re.sub(r'[^a-zA-Z0-9_\-\. ]+', ' ', name).strip()
    ascii_clean = re.sub(r'\s+', ' ', ascii_clean) or "media"
    ascii_filename = f"{ascii_clean[:80]}.{ext}"
    utf8_name = urllib.parse.quote(f"{name[:120]}.{ext}")
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_name}'


def _extract_stream_url(
    url: str,
    type: str,
    quality: str,
    format: str,
    cookiefile: str | None = None,
) -> tuple[str, dict[str, str], str | None, dict[str, str], str, str, bool]:
    """Extract the direct media URL without downloading the file."""
    opts: dict[str, Any] = {}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if type == "video":
        effective_q = quality
        if MAX_VIDEO_HEIGHT and effective_q == "best":
            effective_q = str(MAX_VIDEO_HEIGHT)
        # Prioritize H.264 (avc1/h264) + AAC (mp4a), universally hardware-decoded on 100% of Windows PCs, Macs, iPhones, and Androids without black screens.
        if effective_q == "best":
            opts["format"] = (
                "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                "bestvideo[vcodec^=avc1]+bestaudio/"
                "bestvideo[vcodec^=h264]+bestaudio/"
                "best[vcodec^=avc1]/"
                "best[vcodec^=h264]/"
                "bestvideo[vcodec^=vp9]+bestaudio/"
                "bestvideo[vcodec^=vp09]+bestaudio/"
                "bestvideo+bestaudio/best"
            )
        else:
            h = int(effective_q)
            if h > 1080:
                # 1440p (2K) and 2160p (4K): prefer VP9 (natively supported by Windows/Android) over AV1
                opts["format"] = (
                    f"bestvideo[height<={h}][vcodec^=vp9]+bestaudio/"
                    f"bestvideo[height<={h}][vcodec^=vp09]+bestaudio/"
                    f"bestvideo[height<={h}]+bestaudio/best"
                )
            else:
                # <= 1080p: strictly prioritize H.264 (avc1) for 100% media player compatibility
                opts["format"] = (
                    f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                    f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio/"
                    f"bestvideo[height<={h}][vcodec^=h264]+bestaudio/"
                    f"bestvideo[height<={h}]+bestaudio/"
                    f"best[height<={h}]/"
                    f"bestvideo+bestaudio/best"
                )
    else:
        opts["format"] = "bestaudio[acodec^=mp4a]/bestaudio/best"
    with yt_dlp.YoutubeDL(ydl_options(opts)) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("No media information returned.")
    protocol = str(info.get("protocol") or "")
    direct = info.get("url")
    direct_headers = info.get("http_headers") or {}
    audio_url = None
    audio_headers = {}
    if not direct:
        rf = info.get("requested_formats")
        if rf and len(rf) >= 2:
            direct = rf[0].get("url")
            direct_headers = rf[0].get("http_headers") or {}
            audio_url = rf[1].get("url")
            audio_headers = rf[1].get("http_headers") or {}
            protocol = str(rf[0].get("protocol") or protocol)
        elif rf and len(rf) == 1:
            direct = rf[0].get("url")
            direct_headers = rf[0].get("http_headers") or {}
            protocol = str(rf[0].get("protocol") or protocol)
    if not direct:
        raise RuntimeError("No streamable URL found for this media.")
    is_manifest = bool(
        any(m in protocol.lower() for m in ("m3u8", "dash"))
        or ".m3u8" in direct.lower()
        or ".mpd" in direct.lower()
    )
    return (
        direct,
        direct_headers,
        audio_url,
        audio_headers,
        info.get("title") or "media",
        info.get("ext") or ("mp4" if type == "video" else format),
        is_manifest,
    )


def _stream_error_page(message: str) -> Response:
    """Return a tiny HTML page that notifies the parent frame of the error."""
    escaped = json.dumps(message[:240])
    html = (
        "<html><body><script>"
        f"window.parent.postMessage({{source:'omni-extractor',type:'download-error',error:{escaped}}},'*');"
        "</script></body></html>"
    )
    return Response(content=html, media_type="text/html")


@app.get("/api/stream-download")
async def stream_download(
    http_request: Request,
    url: str = Query(..., min_length=8, max_length=2048),
    type: str = Query("video", pattern="^(video|audio)$"),
    quality: str = Query("best", max_length=8),
    format: str = Query("mp3", pattern="^(mp3|m4a|flac|wav|mp4)$"),
    bitrate: int = Query(320, ge=64, le=320),
    title: str | None = Query(None, max_length=180),
) -> Response:
    """Stream media directly to the client without storing on server disk."""
    try:
        safe_url = validate_public_url(url)
        validate_download_args(type, quality, format)
    except HTTPException as exc:
        return _stream_error_page(exc.detail)

    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    try:
        with linked_cookiefile(token) as cookiefile:
            direct_url, http_headers, audio_url, audio_headers, media_title, src_ext, is_manifest = await run_in_threadpool(
                _extract_stream_url, safe_url, type, quality, format, cookiefile
            )
    except Exception as exc:
        return _stream_error_page(f"Could not extract media: {str(exc)[:200]}")

    final_name = safe_filename(title or media_title)

    if type == "video":
        if (audio_url or is_manifest) and FFMPEG_LOCATION:
            # Video + Audio are separate adaptive streams OR HLS/DASH manifest: live-mux via FFmpeg to client (zero disk)
            v_h = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
            cmd = [FFMPEG_LOCATION, "-hide_banner", "-loglevel", "error"]
            if v_h:
                cmd += ["-headers", v_h]
            cmd += ["-i", direct_url]
            if audio_url:
                a_h = "".join(f"{k}: {v}\r\n" for k, v in audio_headers.items())
                if a_h:
                    cmd += ["-headers", a_h]
                cmd += ["-i", audio_url]
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
                "-y", "pipe:1",
            ]
            try:
                proc = await run_in_threadpool(
                    lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                )
            except Exception as exc:
                return _stream_error_page(f"Could not start video multiplexing: {str(exc)[:200]}")

            async def _video_mux_pipe():
                try:
                    while True:
                        chunk = await run_in_threadpool(proc.stdout.read, 256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.stdout.close()
                    proc.wait()

            return StreamingResponse(
                _video_mux_pipe(),
                media_type="video/mp4",
                headers={
                    "Content-Disposition": make_content_disposition(final_name, "mp4"),
                    "Cache-Control": "no-store",
                },
            )
        else:
            # Single pre-merged stream: direct proxy-stream via urllib
            try:
                req = urllib.request.Request(direct_url, headers=http_headers)
                source = await run_in_threadpool(lambda: urllib.request.urlopen(req, timeout=60))
            except Exception as exc:
                return _stream_error_page(f"Could not connect to media source: {str(exc)[:200]}")

            ctype = str(source.headers.get("Content-Type", "")).lower()
            if any(m in ctype for m in ("mpegurl", "m3u8", "dash+xml")) and FFMPEG_LOCATION:
                source.close()
                v_h = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
                cmd = [FFMPEG_LOCATION, "-hide_banner", "-loglevel", "error"]
                if v_h:
                    cmd += ["-headers", v_h]
                cmd += [
                    "-i", direct_url,
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    "-f", "mp4",
                    "-y", "pipe:1",
                ]
                proc = await run_in_threadpool(
                    lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                )
                async def _video_mux_pipe_fb():
                    try:
                        while True:
                            chunk = await run_in_threadpool(proc.stdout.read, 256 * 1024)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        proc.stdout.close()
                        proc.wait()

                return StreamingResponse(
                    _video_mux_pipe_fb(),
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": make_content_disposition(final_name, "mp4"),
                        "Cache-Control": "no-store",
                    },
                )

            content_length = source.headers.get("Content-Length")

            async def _video_pipe():
                try:
                    while True:
                        chunk = await run_in_threadpool(source.read, 256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    source.close()

            headers: dict[str, str] = {
                "Content-Disposition": make_content_disposition(final_name, src_ext),
                "Cache-Control": "no-store",
            }
            if content_length:
                headers["Content-Length"] = content_length
            return StreamingResponse(_video_pipe(), media_type="video/mp4", headers=headers)

    else:
        # Audio: pipe source URL through FFmpeg for format conversion (zero disk)
        if not FFMPEG_LOCATION:
            return _stream_error_page("FFmpeg is required for audio conversion but is not installed.")

        codec_map: dict[str, list[str]] = {
            "mp3": ["-codec:a", "libmp3lame", "-b:a", f"{bitrate}k", "-f", "mp3"],
            "m4a": ["-codec:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "ipod"],
            "flac": ["-codec:a", "flac", "-f", "flac"],
            "wav": ["-codec:a", "pcm_s16le", "-f", "wav"],
        }
        codec_args = codec_map.get(format, codec_map["mp3"])

        # Pass the required HTTP headers so FFmpeg can fetch from the CDN
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
        cmd = [FFMPEG_LOCATION, "-hide_banner", "-loglevel", "error"]
        if headers_str:
            cmd += ["-headers", headers_str]
        cmd += ["-i", direct_url, "-vn", *codec_args, "-y", "pipe:1"]

        try:
            proc = await run_in_threadpool(
                lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            )
        except Exception as exc:
            return _stream_error_page(f"Could not start audio conversion: {str(exc)[:200]}")

        async def _audio_pipe():
            try:
                while True:
                    chunk = await run_in_threadpool(proc.stdout.read, 256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.stdout.close()
                proc.wait()

        ext_clean = format.lower()
        mime_map = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "flac": "audio/flac", "wav": "audio/wav"}
        return StreamingResponse(
            _audio_pipe(),
            media_type=mime_map.get(format, "application/octet-stream"),
            headers={
                "Content-Disposition": make_content_disposition(final_name, ext_clean),
                "Cache-Control": "no-store",
            },
        )


@app.post("/api/extract-batch")
async def extract_batch(http_request: Request, payload: BatchRequest) -> dict[str, Any]:
    urls = re.findall(r'https?://[^\s<>"\']+', payload.text)
    urls = list(dict.fromkeys(u.rstrip(".,);]") for u in urls))[:10]
    results = []
    token = http_request.cookies.get(SESSION_COOKIE_NAME)
    with linked_cookiefile(token) as cookiefile:
        for url in urls:
            try:
                safe_url = validate_public_url(url)
                data = await run_in_threadpool(extract_info, safe_url, cookiefile)
                results.append({"url": url, "ok": True, "data": data})
            except Exception as exc:
                results.append({"url": url, "ok": False, "error": str(exc)[:200]})
    return {"results": results, "count": len(results), "limit": 10}


@app.get("/api/network-info")
async def network_info() -> dict[str, Any]:
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        local_ip = "127.0.0.1"
    return {"local_ip": local_ip, "port": int(os.getenv("PORT", "8000")), "public_tunnel": False}


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
