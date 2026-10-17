from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
_JOB_SLOTS = threading.Semaphore(MAX_CONCURRENT_JOBS)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


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
    extractor_args = parse_extractor_args(os.getenv("YTDLP_EXTRACTOR_ARGS", ""))
    if extractor_args:
        opts["extractor_args"] = extractor_args
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if extra:
        opts.update(extra)
    return opts


def clean_metadata(info: dict[str, Any]) -> dict[str, Any]:
    formats: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in info.get("formats") or []:
        height = item.get("height")
        if not height or item.get("vcodec") == "none":
            continue
        height = int(height)
        # One representative format per height; use the best bitrate/size estimate.
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
        if existing is None:
            formats.append(candidate)
        elif size and (not existing.get("filesize") or size > existing["filesize"]):
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


def extract_info(raw_url: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_options()) as ydl:
        info = ydl.extract_info(raw_url, download=False)
        if not info:
            raise RuntimeError("No media information was returned.")
        return clean_metadata(info)


class InfoRequest(BaseModel):
    url: str


class BatchRequest(BaseModel):
    text: str


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "ffmpeg": bool(FFMPEG_LOCATION)}


@app.get("/api/info")
async def info_get(url: str = Query(..., min_length=8, max_length=2048)) -> dict[str, Any]:
    safe_url = validate_public_url(url)
    try:
        return await run_in_threadpool(extract_info, safe_url)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read this media URL: {str(exc)[:240]}")


@app.post("/api/info")
async def info_post(request: InfoRequest) -> dict[str, Any]:
    return await info_get(request.url)


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
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "outtmpl": os.path.join(tempdir, "%(title).150B.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
    }
    if type == "video":
        if quality == "best":
            options["format"] = "bestvideo+bestaudio/best"
        else:
            max_height = int(quality)
            options["format"] = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
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
    options = build_download_options(tempdir, type, quality, format, bitrate)

    try:
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


def _run_download_job(job_id: str, safe_url: str, type: str, quality: str, format: str, bitrate: int, title: str | None, tempdir: str) -> None:
    with _JOB_SLOTS:
        _update_job(job_id, status="working", stage="downloading")
        options = build_download_options(tempdir, type, quality, format, bitrate, _progress_hook(job_id))
        try:
            _download_sync(safe_url, options)
            path, size = finish_download(tempdir, type, format, title)
            _update_job(job_id, status="done", stage="done", path=str(path), filename=path.name, size=size, tempdir=tempdir)
        except Exception as exc:
            shutil.rmtree(tempdir, ignore_errors=True)
            _update_job(job_id, status="error", path=None, error=download_error_message(exc, type), tempdir=None)


JOB_PUBLIC_FIELDS = ("status", "stage", "error", "filename", "size", "downloaded", "total")


@app.post("/api/download-job")
async def download_job(
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

    tempdir = tempfile.mkdtemp(prefix="omni-")
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        queued_ahead = sum(1 for job in DOWNLOAD_JOBS.values() if job["status"] in {"queued", "working"})
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
    task = asyncio.create_task(run_in_threadpool(_run_download_job, job_id, safe_url, type, quality, format, bitrate, title, tempdir))
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


@app.get("/api/download-job/{job_id}/file")
async def download_job_file(job_id: str) -> FileResponse:
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

    def _cleanup() -> None:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        with JOBS_LOCK:
            DOWNLOAD_JOBS.pop(job_id, None)

    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(_cleanup),
    )


def _download_sync(url: str, options: dict[str, Any]) -> None:
    with yt_dlp.YoutubeDL(ydl_options(options)) as ydl:
        ydl.download([url])


@app.post("/api/extract-batch")
async def extract_batch(request: BatchRequest) -> dict[str, Any]:
    urls = re.findall(r'https?://[^\s<>"\']+', request.text)
    urls = list(dict.fromkeys(u.rstrip(".,);]") for u in urls))[:10]
    results = []
    for url in urls:
        try:
            safe_url = validate_public_url(url)
            data = await run_in_threadpool(extract_info, safe_url)
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
