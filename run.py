from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Omni Extractor")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN access)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser tab")
    args = parser.parse_args()

    if not args.no_browser and args.host in {"127.0.0.1", "localhost"}:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(f"http://{args.host}:{args.port}")), daemon=True).start()
    uvicorn.run("app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
