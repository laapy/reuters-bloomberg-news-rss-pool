#!/usr/bin/env python3
"""Start, inspect, or stop the local HTTP service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PID_FILE = DATA / "http_service.pid"
BASE_URL = "http://127.0.0.1:8765"


def request_json(path: str, method: str = "GET", timeout: float = 3.0):
    request = urllib.request.Request(BASE_URL + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def health():
    try:
        return request_json("/api/health")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def start() -> int:
    current = health()
    if current:
        print(json.dumps({"status": "already_running", "url": BASE_URL + "/",
                          "pool": current.get("pool", {})}, ensure_ascii=False, indent=2))
        return 0
    DATA.mkdir(parents=True, exist_ok=True)
    out_handle = open(DATA / "http_service.out.log", "ab", buffering=0)
    err_handle = open(DATA / "http_service.err.log", "ab", buffering=0)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008) |
                          getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) |
                          getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--host", "127.0.0.1",
         "--port", "8765", "--update-interval", "1800"],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out_handle, stderr=err_handle,
        close_fds=True, creationflags=creation_flags,
    )
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    for _ in range(30):
        time.sleep(0.25)
        current = health()
        if current:
            print(json.dumps({"status": "started", "pid": process.pid,
                              "url": BASE_URL + "/", "pool": current.get("pool", {})},
                             ensure_ascii=False, indent=2))
            return 0
        if process.poll() is not None:
            break
    print(json.dumps({"status": "start_error", "pid": process.pid,
                      "log": str(DATA / "http_service.err.log")},
                     ensure_ascii=False, indent=2))
    return 1


def status() -> int:
    current = health()
    if current:
        print(json.dumps({"status": "running", "url": BASE_URL + "/", **current},
                         ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"status": "stopped"}, ensure_ascii=False, indent=2))
    return 1


def stop() -> int:
    current = health()
    if not current:
        PID_FILE.unlink(missing_ok=True)
        print(json.dumps({"status": "already_stopped"}, ensure_ascii=False, indent=2))
        return 0
    request_json("/api/shutdown", method="POST", timeout=5)
    for _ in range(30):
        time.sleep(0.2)
        if not health():
            PID_FILE.unlink(missing_ok=True)
            print(json.dumps({"status": "stopped"}, ensure_ascii=False, indent=2))
            return 0
    print(json.dumps({"status": "stop_pending"}, ensure_ascii=False, indent=2))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "status", "stop"))
    args = parser.parse_args()
    return {"start": start, "status": status, "stop": stop}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
