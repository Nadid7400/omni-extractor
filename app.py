from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any
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
    if type == "audio" and format == "mp4":
        raise HTTPException(status_code=400, detail="Choose an audio format.")
    if type == "video" and format != "mp4":
        raise HTTPException(status_code=400, detail="Choose MP4 for video downloads.")

    tempdir = tempfile.mkdtemp(prefix="omni-")
    options: dict[str, Any] = {
        "outtmpl": os.path.join(tempdir, "%(title).150B.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
    }
    if type == "video":
        if quality == "best":
            options["format"] = "bestvideo+bestaudio/best"
        else:
            try:
                max_height = int(quality)
                if max_height not in {144, 240, 360, 480, 720, 1080, 1440, 2160}:
                    raise ValueError
            except ValueError:
                shutil.rmtree(tempdir, ignore_errors=True)
                raise HTTPException(status_code=400, detail="Unsupported video quality.")
            options["format"] = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        options["merge_output_format"] = "mp4"
    else:
        options["format"] = "bestaudio/best"
        if format == "mp3":
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)}]
        elif format in {"m4a", "flac", "wav"}:
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": format}]

    try:
        await run_in_threadpool(lambda: _download_sync(safe_url, options))
        path = find_downloaded_file(tempdir)
        if title:
            suffix = ".mp4" if type == "video" else f".{format}"
            path = path.rename(path.with_name(safe_filename(title) + suffix))
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
        message = str(exc)
        if not FFMPEG_LOCATION and ("ffmpeg" in message.lower() or type == "audio"):
            message = "FFmpeg is required for this conversion. Install ffmpeg or imageio-ffmpeg."
        raise HTTPException(status_code=422, detail=f"Download failed: {message[:240]}")


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
