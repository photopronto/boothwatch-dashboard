#!/usr/bin/env python3
"""
BoothWatch launcher: keeps agent.py running and keeps it up to date.

This file is deliberately small and should almost never change. All the
interesting work happens in agent.py, which you publish from the dashboard.

Every few minutes it asks the dashboard which agent version this booth should
run (test or live channel). If it differs from the local copy it downloads the
new one, checks it, swaps it in and restarts the agent. If the new version
crashes within the first minute and a half it puts the previous one back and
remembers not to try that version again.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
AGENT = BASE / "agent.py"
PREV = BASE / "agent.prev.py"
NEW = BASE / "agent.new.py"
BAD_PATH = BASE / "bad_releases.txt"
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "agent_state.json"
LOG_PATH = BASE / "launcher.log"

log = logging.getLogger("launcher")


def setup_logging():
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=300_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        log.addHandler(logging.StreamHandler())


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_token():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f).get("agent_token")
    except Exception:
        return None


def rpc(cfg, fn, payload):
    key = cfg["supabase_anon_key"]
    r = requests.post(
        f"{cfg['supabase_url']}/rest/v1/rpc/{fn}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"{fn} failed ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content else None


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bad_releases():
    try:
        return set(BAD_PATH.read_text().split())
    except Exception:
        return set()


def mark_bad(sha):
    with open(BAD_PATH, "a") as f:
        f.write(sha + "\n")


def start_agent():
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen([sys.executable, str(AGENT)], cwd=str(BASE), creationflags=flags)


def stop_agent(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def check_for_update(cfg, child):
    """If a different version is published for this booth's channel, install it.
    Returns the new version's hash if installed, otherwise None."""
    token = load_token()
    if not token:
        return None
    poll = rpc(cfg, "agent_poll", {"p_token": token})
    target = poll.get("release_sha256")
    if not target:
        return None                      # nothing published for this channel yet
    if target in bad_releases():
        return None                      # this one already failed on this booth
    local = sha_of(AGENT.read_bytes()) if AGENT.exists() else None
    if local == target:
        return None

    code = rpc(cfg, "fetch_agent_code", {"p_token": token})
    if not isinstance(code, str):
        raise RuntimeError("no code returned")
    data = code.encode("utf-8")
    if sha_of(data) != target:
        raise RuntimeError("downloaded code does not match its checksum, ignoring")
    compile(code, "agent.py", "exec")    # refuse anything that is not valid Python

    NEW.write_bytes(data)
    if AGENT.exists():
        shutil.copy2(AGENT, PREV)
    os.replace(NEW, AGENT)
    log.info("Installed agent version %s (%s channel)", poll.get("release_version"), poll.get("channel"))
    stop_agent(child)
    return target


def main():
    setup_logging()
    cfg = load_config()
    check_every = int(cfg.get("launcher_check_seconds", 300))
    health_window = int(cfg.get("launcher_health_seconds", 90))

    child = None
    started = 0.0
    pending = None          # hash of a freshly installed version we have not yet trusted
    last_check = 0.0
    log.info("Launcher started")

    while True:
        if child is not None and child.poll() is not None:
            ran_for = time.time() - started
            log.info("Agent exited (code %s) after %.0fs", child.returncode, ran_for)
            rolled_back = False
            if pending and ran_for < health_window and PREV.exists():
                log.info("New version did not stay up, putting the previous one back")
                mark_bad(pending)
                shutil.copy2(PREV, AGENT)
                rolled_back = True
            pending = None
            child = None
            if not rolled_back:
                time.sleep(5 if ran_for > 10 else 30)    # avoid a tight crash loop

        if child is None:
            child = start_agent()
            started = time.time()
            log.info("Agent started")

        if pending and child.poll() is None and time.time() - started >= health_window:
            log.info("New version is healthy")
            pending = None

        if time.time() - last_check >= check_every:
            last_check = time.time()
            try:
                installed = check_for_update(cfg, child)
                if installed:
                    pending = installed
                    child = None         # restarted at the top of the loop
            except Exception as e:
                log.info("Update check failed: %s", e)

        time.sleep(2)


if __name__ == "__main__":
    main()
