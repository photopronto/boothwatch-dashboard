#!/usr/bin/env python3
"""
BoothWatch agent for Windows photobooth PCs.

Checks that your booth programs are running and healthy, reports what each
program's window is doing, counts sessions and prints, and carries out the
buttons you press on the dashboard (restart a program, restart the PC, show a
closed message, send a screenshot).

  python agent.py                 run normally (first run shows a pairing code)
  python agent.py --discover      list running programs, to fill in config.json
  python agent.py --once          do one check and print it, send nothing
  python agent.py --stats         show what it can read for counting sessions and prints
  python agent.py --startup       show how Windows starts each program at sign-in
  python agent.py --devices       show what it can read from the printer and camera
  python agent.py --watch-printer print a line each time the printer's status changes
  python agent.py --test-closed   show the closed message for 8 seconds
  python agent.py --screenshot    save a test screenshot to screenshot-test.jpg
  python agent.py --pair          make a new pairing code (if the old one is lost)

Normally this file is started by launcher.py, which keeps it up to date.
"""
import argparse
import base64
import ctypes
import datetime
import io
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

try:
    import win32print
except ImportError:
    win32print = None

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    HAVE_WIN32 = True
except ImportError:  # lets the logic be tested off Windows
    HAVE_WIN32 = False

VERSION = "0.10.0"

# Programs whose window carries the booth's running print counter
# ("PhotoboothStatus: ... printCounter=829 ..."). Counting needs this.
DEFAULT_READ_COUNTER = {"DSLR Remote Pro"}

COUNTER_RE = re.compile(r"printCounter\s*=\s*(\d+)", re.I)
# e.g. C:\Users\hello\Documents\PhotoboothImages\2026-10-08\prints\261008_222542.jpg
IMAGES_ROOT_RE = re.compile(r"([A-Za-z]:\\[^\r\n]*?PhotoboothImages)\\\d{4}-\d{2}-\d{2}\\", re.I)
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

# Programs that are closed while the booth is shut, so the card machine switches itself off
# (MDB Payment disables the reader whenever the booth software is not running full screen).
DEFAULT_STOP_WHEN_CLOSED = {"DSLR Remote Pro"}

DEFAULT_CLOSED_MESSAGE = "Sorry, this photo booth is closed right now."
CREATE_NO_WINDOW = 0x08000000
DETACHED_NEW_GROUP = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# The window that shows the photobooth screen. Its title ends with the screen's
# file name, so the agent can report which screen is showing.
DEFAULT_MAIN_WINDOW = {
    "DSLR Remote Pro": {"title_starts_with": "Breeze Systems Photobooth"},
}

# Lines to show on the dashboard for a program, read from its window.
# Used when config.json does not say otherwise, so a new agent version is enough.
DEFAULT_SHOW_TEXT = {
    "MDB Payment": [
        {"label": "MDB status", "starts_with": "MDB Status:", "strip_prefix": True},
        {"label": "Booth software", "starts_with": "DSLR Remote Pro"},
    ],
}
BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "agent_state.json"      # holds this booth's secret token
CODE_PATH = BASE / "pairing_code.txt"
LOG_PATH = BASE / "boothwatch.log"
EVENTS_PATH = BASE / "events.json"                  # event timeline waiting to be sent, and what was already seen
RUNTIME_PATH = BASE / "runtime.json"                # last time the agent was alive, and outages not yet reported
REMOTE_CACHE_PATH = BASE / "remote_cache.json"      # last settings from the dashboard (kept if the internet drops)
CLOSED_MESSAGE_PATH = BASE / "closed_message.txt"   # text shown on the closed screen

POLL_SECONDS = 10          # how often to ask the dashboard for settings / screenshot requests

log = logging.getLogger("boothwatch")


# ───────────────────────── setup helpers ─────────────────────────

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def make_dpi_aware():
    """So window sizes and screenshots use real pixels on scaled displays."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("auto_restart", False)
    cfg.setdefault("programs", [])
    return apply_defaults(cfg)


def apply_defaults(cfg):
    for prog in cfg.get("programs", []):
        if "show_window_text" not in prog and prog.get("name") in DEFAULT_SHOW_TEXT:
            prog["show_window_text"] = DEFAULT_SHOW_TEXT[prog["name"]]
        if "main_window" not in prog and prog.get("name") in DEFAULT_MAIN_WINDOW:
            prog["main_window"] = DEFAULT_MAIN_WINDOW[prog["name"]]
        if "read_print_counter" not in prog and prog.get("name") in DEFAULT_READ_COUNTER:
            prog["read_print_counter"] = True
        if "stop_when_closed" not in prog and prog.get("name") in DEFAULT_STOP_WHEN_CLOSED:
            prog["stop_when_closed"] = True
    return cfg


_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_time(text):
    h, m = str(text).split(":")[:2]
    return datetime.time(int(h) % 24, int(m)) if int(h) < 24 else datetime.time(23, 59, 59, 999999)


def _parse_stamp(text):
    """'2026-10-10 13:00:00+01' (how the database writes it) or ISO, to an aware datetime."""
    t = str(text).strip().replace(" ", "T")
    t = re.sub(r"([+-]\d\d)$", r"\1:00", t)
    return datetime.datetime.fromisoformat(t)


def in_opening_hours(hours, now=None):
    """Mirror of the database's booth_in_hours(): is the booth inside its weekly opening hours?
    hours = {"mode": ..., "days": {"mon": ["09:00", "18:00"], "tue": null, ...}, "open_until": ...}"""
    if not isinstance(hours, dict) or hours.get("mode") not in ("auto_close", "pause_alerts"):
        return True
    now = now or datetime.datetime.now()
    try:
        until = hours.get("open_until")
        if isinstance(until, str):
            aware = now if now.tzinfo else now.astimezone()
            if _parse_stamp(until) > aware:
                return True
    except Exception:
        pass
    days = hours.get("days") if isinstance(hours.get("days"), dict) else {}
    t = now.time()
    dow = now.weekday()                          # Monday = 0

    def window(index):
        w = days.get(_DAY_KEYS[index % 7])
        if isinstance(w, list) and len(w) == 2:
            try:
                return _parse_time(w[0]), (datetime.time(23, 59, 59, 999999) if str(w[1]) == "24:00" else _parse_time(w[1])), str(w[1]) == "24:00"
            except Exception:
                return None
        return None

    today = window(dow)
    if today:
        o, c, midnight = today
        if midnight or c > o:
            if o <= t < c or (midnight and t >= o):
                return True
        elif t >= o:
            return True                          # closes after midnight
    prev = window(dow - 1)
    if prev:
        o, c, midnight = prev
        if not midnight and c <= o and t < c:
            return True
    return False


def is_payment_program(prog):
    name = str(prog.get("name", "")).lower()
    return "payment" in name or "mdb" in name


def watching(cfg, what):
    """False when this booth has been told (on the dashboard) not to watch its printer, camera or payment software."""
    return (cfg.get("watch") or {}).get(what, True) is not False


def effective_config(local, remote):
    """Local config.json, with the settings you changed on the dashboard on top.
    Restart commands never come from the dashboard, only from config.json."""
    cfg = dict(local)
    cfg["stop_software"] = (local.get("closed_screen") or {}).get("stop_software", True) is not False
    if not isinstance(remote, dict):
        return cfg
    cfg["scheduled_closed"] = False
    hours = remote.get("hours")
    if isinstance(hours, dict) and hours.get("mode") == "auto_close" and not in_opening_hours(hours):
        cfg["scheduled_closed"] = True            # outside opening hours
    cfg["closed"] = remote.get("closed") is True or cfg["scheduled_closed"]
    if isinstance(remote.get("auto_restart"), bool):
        cfg["auto_restart"] = remote["auto_restart"]
    iv = remote.get("interval_seconds")
    if isinstance(iv, int) and not isinstance(iv, bool) and iv >= 15:
        cfg["interval_seconds"] = iv
    rp = remote.get("programs")
    if isinstance(rp, list) and rp:
        local_by_name = {p.get("name"): p for p in local.get("programs", [])}
        merged = []
        for p in rp:
            if not isinstance(p, dict) or "name" not in p:
                continue
            q = {k: v for k, v in p.items() if k not in ("restart_command", "restart_folder")}
            lp = local_by_name.get(p["name"], {})
            for k in ("restart_command", "restart_folder"):
                if k in lp:
                    q[k] = lp[k]
            merged.append(q)
        if merged:
            cfg["programs"] = merged
    cfg = apply_defaults(cfg)
    watch = remote.get("watch")
    if isinstance(watch, dict):
        cfg["watch"] = {k: watch.get(k) is not False for k in ("printer", "camera", "payment")}
        if not cfg["watch"]["payment"]:
            cfg["programs"] = [p for p in cfg["programs"] if not is_payment_program(p)]
    return cfg


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def rpc(cfg, fn, payload):
    key = cfg["supabase_anon_key"]
    r = requests.post(
        f"{cfg['supabase_url']}/rest/v1/rpc/{fn}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"{fn} failed ({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else None


def pair(cfg, state):
    rows = rpc(cfg, "create_pairing", {})
    row = rows[0] if isinstance(rows, list) else rows
    state["agent_token"] = row["agent_token"]
    save_state(state)
    code = row["pairing_code"]
    CODE_PATH.write_text(f"Pairing code: {code}\nEnter it on the dashboard under 'Add booth'.\n")
    print("\n" + "=" * 46)
    print(f"   PAIRING CODE:   {code}")
    print("   Type this into the dashboard: Add booth")
    print("=" * 46 + "\n")
    log.info("Created pairing code %s", code)


# ───────────────────────── process checks ─────────────────────────

_desc_cache = {}


def file_description(path):
    """The 'File description' Windows shows in Task Manager."""
    if not HAVE_WIN32 or not path:
        return ""
    if path in _desc_cache:
        return _desc_cache[path]
    desc = ""
    try:
        lang, cp = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        desc = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription") or ""
    except Exception:
        pass
    _desc_cache[path] = desc
    return desc


def scan_processes():
    procs = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        info = p.info
        exe = info.get("exe") or ""
        procs.append({
            "pid": info["pid"],
            "name": info.get("name") or "",
            "exe": exe,
            "desc": file_description(exe),
        })
    return procs


def find_matches(prog, procs):
    needles = [m.lower() for m in prog.get("match", []) if m]
    out = []
    for p in procs:
        hay = (p["name"] + " " + p["desc"]).lower()
        if any(n in hay for n in needles):
            out.append(p)
    return out


# ───────────────────────── Windows window helpers ─────────────────────────

if HAVE_WIN32:
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E


def _hwnd_text(hwnd):
    n = _user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, None)
    if n <= 0 or n > 4000:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.SendMessageW(hwnd, WM_GETTEXT, n + 1, buf)
    return buf.value


def top_windows(pids, visible_only=True):
    found = []

    def cb(hwnd, _):
        if not visible_only or win32gui.IsWindowVisible(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in pids:
                found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


def window_texts(hwnd):
    texts = [_hwnd_text(hwnd)]

    def cb(child, _):
        texts.append(_hwnd_text(child))
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return [t for t in texts if t]


def choose_window(candidates, title_prefix=None):
    """candidates: [(hwnd, title, area)] in screen order, topmost first.
    Prefer the topmost window whose title starts with title_prefix (the photobooth
    screen window), otherwise the largest window."""
    if title_prefix:
        p = title_prefix.lower()
        for hwnd, title, _area in candidates:
            if title.lower().startswith(p):
                return hwnd
    best, best_area = None, -1
    for hwnd, _title, area in candidates:
        if area > best_area:
            best, best_area = hwnd, area
    return best


def screen_name_from_title(title):
    """'Breeze Systems Photobooth - C:\\...\\Image Screens\\welcome.jpg' -> 'welcome'.
    Returns None if the title does not end in a file path."""
    if " - " not in title or "\\" not in title:
        return None
    filename = title.rsplit("\\", 1)[-1].strip()
    if not filename:
        return None
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return base.strip() or None


def window_info(windows, title_prefix=None):
    """Title and screen state of the program's main window."""
    candidates = []
    for h in windows:
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            candidates.append((h, win32gui.GetWindowText(h) or "", max(0, r - l) * max(0, b - t)))
        except Exception:
            continue
    best = choose_window(candidates, title_prefix)
    if best is None:
        return None
    l, t, r, b = win32gui.GetWindowRect(best)
    minimized = bool(win32gui.IsIconic(best))
    fullscreen = False
    if not minimized:
        mon = win32api.GetMonitorInfo(
            win32api.MonitorFromWindow(best, win32con.MONITOR_DEFAULTTONEAREST))["Monitor"]
        fullscreen = (l <= mon[0] + 2 and t <= mon[1] + 2 and r >= mon[2] - 2 and b >= mon[3] - 2)
    return {
        "title": (win32gui.GetWindowText(best) or "")[:120],
        "fullscreen": fullscreen,
        "minimized": minimized,
        "foreground": win32gui.GetForegroundWindow() == best,
    }


def single_line_texts(texts):
    """Keep short one-line texts (labels). A multi-line box such as a log of
    old messages is ignored, so an old message can't look like the current one."""
    out = []
    for t in texts:
        t = (t or "").strip()
        if t and "\n" not in t and "\r" not in t and len(t) <= 400:
            out.append(t)
    return out


def extract_details(lines, rules):
    """Pick out the lines you asked to see, for example the MDB status."""
    out = {}
    for rule in rules:
        prefix = rule.get("starts_with", "")
        if not prefix:
            continue
        for line in lines:
            if line.lower().startswith(prefix.lower()):
                value = line[len(prefix):].strip() if rule.get("strip_prefix") else line
                out[rule["label"]] = value[:200]
                break
    return out


def parse_booth_status(texts):
    """From the booth window's texts: (print counter or None, photo folder or None)."""
    counter, root = None, None
    for t in texts:
        t = t or ""
        m = COUNTER_RE.search(t)
        if m:
            counter = int(m.group(1))
        r = IMAGES_ROOT_RE.search(t)
        if r:
            root = r.group(1)
    return counter, root


def count_sessions(images_root, day):
    """Sessions today = pictures saved in <photo folder>\\<day>\\prints.
    Returns None when that day's folder does not exist yet or cannot be read."""
    folder = Path(images_root) / day / "prints"
    try:
        return sum(1 for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    except OSError:
        return None


class PrintMatcher:
    """Every session should lead to a print. Each new session picture is noted, and each new print
    counted by the booth software clears the oldest one. A session still waiting for its print
    after the grace period is reported as a possible missed print."""
    def __init__(self):
        self.day = None
        self.sessions = None
        self.counter = None
        self.owed = []          # when each unmatched session was first seen

    def update(self, day, sessions, counter, grace_minutes, now=None):
        now = time.time() if now is None else now
        if sessions is None or counter is None:
            return 0            # cannot tell, so never raise an alarm
        if day != self.day or self.sessions is None or sessions < self.sessions or counter < self.counter:
            self.day, self.sessions, self.counter, self.owed = day, sessions, counter, []
            return 0            # first look (or a new day / reset): start counting from here
        new_sessions = sessions - self.sessions
        new_prints = counter - self.counter
        self.sessions, self.counter = sessions, counter
        self.owed += [now] * new_sessions
        del self.owed[:new_prints]      # a print settles the oldest waiting session; extra prints are ignored
        grace = max(1, grace_minutes) * 60
        return sum(1 for t in self.owed if now - t >= grace)


def resolve_images_root(local_cfg, state):
    """Where the booth saves photos: config.json, else learned from the booth window, else the usual place."""
    explicit = local_cfg.get("images_folder")
    if explicit:
        return os.path.expandvars(os.path.expanduser(explicit))
    if state.get("images_root"):
        return state["images_root"]
    return os.path.join(os.path.expanduser("~"), "Documents", "PhotoboothImages")


def check_program(prog, procs):
    """Returns {name, running, ok, note, title, fullscreen, minimized, details}."""
    name = prog["name"]
    matches = find_matches(prog, procs)
    if not matches:
        return {"name": name, "running": False, "ok": False, "note": "Not running"}

    result = {"name": name, "running": True, "ok": True, "note": ""}
    if not HAVE_WIN32:
        return result

    pids = {m["pid"] for m in matches}
    problems = []
    try:
        windows = top_windows(pids)
        if any(_user32.IsHungAppWindow(h) for h in windows):
            result.update(ok=False, note="Not responding")
            return result

        if prog.get("read_print_counter"):
            # The counter sits in a hidden control, so include hidden windows too.
            raw = [t for h in top_windows(pids, visible_only=False) for t in window_texts(h)]
            counter, images_root = parse_booth_status(raw)
            if counter is not None:
                result["_print_counter"] = counter      # removed before reporting
            if images_root:
                result["_images_root"] = images_root

        prefix = (prog.get("main_window") or {}).get("title_starts_with")
        info = window_info(windows, prefix)
        if info:
            screen = screen_name_from_title(info["title"]) if prefix else None
            if screen:
                result["screen"] = screen          # e.g. "welcome" or "share"
            else:
                result["title"] = info["title"]
            result["fullscreen"] = info["fullscreen"]
            result["minimized"] = info["minimized"]

        alert_rules = prog.get("alert_if_window_text_contains", [])
        show_rules = prog.get("show_window_text", [])
        if alert_rules or show_rules:
            lines = single_line_texts([t for h in windows for t in window_texts(h)])
            for rule in alert_rules:
                needle = rule["text"].lower()
                if any(needle in line.lower() for line in lines):
                    problems.append(rule.get("note") or rule["text"])
            details = extract_details(lines, show_rules)
            if details:
                result["details"] = details

        if prog.get("expect_fullscreen") and info and not info["fullscreen"]:
            problems.append("Minimised" if info["minimized"] else "Not in full screen mode")
    except Exception as e:
        log.info("Window check failed for %s: %s", name, e)

    if problems:
        result.update(ok=False, note="; ".join(problems))
    return result


# ───────────────────────── screenshot ─────────────────────────

def take_screenshot_b64():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ───────────────────────── starting and restarting programs ─────────────────────────
#
# A program is restarted the same way Windows starts it when the booth signs in
# (a Startup-folder shortcut or script, or a "Run" registry entry). That way it
# comes back exactly as it normally starts, for example straight into booth mode.
# If no such entry is found, the plain program path from config.json is used.

SCRIPT_EXTS = {".bat", ".cmd", ".ahk", ".vbs", ".ps1"}


def _read_head(path, limit=20000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def _shortcut_target(path):
    try:
        import win32com.client
        sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path))
        return f"{sc.TargetPath or ''} {sc.Arguments or ''}".strip()
    except Exception:
        return ""


def startup_entries():
    """Things Windows starts at sign-in:
    [{"source", "match_text", "launch", "cwd"}], Startup-folder items first."""
    entries = []
    if os.name != "nt":
        return entries

    folders = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs\StartUp"),
    ]
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            ext = os.path.splitext(name)[1].lower()
            if ext == ".lnk":
                text = f"{name} {_shortcut_target(path)}"
            elif ext in SCRIPT_EXTS:
                text = f"{name} {_read_head(path)}"
            elif ext == ".exe":
                text = name
            else:
                continue
            entries.append({
                "source": f"Startup folder item '{name}'",
                "match_text": text,
                "launch": f'cmd /c start "" "{path}"',
                "cwd": folder if ext != ".lnk" else None,
            })

    try:
        import winreg
        keys = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
        ]
        for hive, sub in keys:
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    entries.append({
                        "source": f"Windows startup entry '{name}'",
                        "match_text": str(value),
                        "launch": str(value),
                        "cwd": None,
                    })
    except ImportError:
        pass
    return entries


def pick_startup_entry(entries, needles):
    needles = [n.lower() for n in needles if n]
    for e in entries:
        text = e["match_text"].lower()
        if any(n in text for n in needles):
            return e
    return None


def resolve_restart(prog):
    """(command, working folder, how it was found) for starting this program, or (None, None, None)."""
    if prog.get("restart_mode", "startup_entry") == "startup_entry":
        entry = pick_startup_entry(startup_entries(), prog.get("match", []))
        if entry:
            return entry["launch"], entry.get("cwd"), entry["source"]
    cmd = prog.get("restart_command")
    if cmd:
        return cmd, prog.get("restart_folder") or None, "the program's own path, which may not start it in booth mode"
    return None, None, None


def launch(cmd, cwd=None):
    flags = DETACHED_NEW_GROUP if os.name == "nt" else 0
    subprocess.Popen(cmd, shell=True, cwd=cwd or None, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_program(prog):
    """Close the program's processes. Returns how many were running."""
    mine = os.getpid()
    victims = []
    for m in find_matches(prog, scan_processes()):
        if m["pid"] == mine:
            continue
        try:
            p = psutil.Process(m["pid"])
            p.terminate()
            victims.append(p)
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=8)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)
    return len(victims)


def restart_program(cfg, name):
    """Stop a configured program and start it again. Returns (ok, text for the dashboard)."""
    prog = next((p for p in cfg["programs"] if p.get("name") == name), None)
    if prog is None:
        return False, f"'{name}' is not set up on this booth"
    cmd, cwd, how = resolve_restart(prog)
    if not cmd:
        return False, "I don't know how to start it again, so I left it running. Set restart_command in config.json."
    stopped = stop_program(prog)
    time.sleep(2)
    launch(cmd, cwd)
    for _ in range(30):
        time.sleep(1)
        if find_matches(prog, scan_processes()):
            return True, f"Restarted{'' if stopped else ' (it was not running)'}. Started using {how}."
    return False, f"Closed it and tried to start it using {how}, but it was not running 30 seconds later."


def run_restart_pc():
    """Restart Windows in 15 seconds, closing anything that is stuck."""
    if os.name != "nt":
        raise RuntimeError("Restarting the PC only works on Windows")
    subprocess.run(["shutdown", "/r", "/f", "/t", "15", "/c", "BoothWatch: restart requested from the dashboard"],
                   check=True, capture_output=True)


# ───────────────────────── auto-restart ─────────────────────────

_down_cycles = {}
_restart_times = {}


def maybe_restart(cfg, prog, status):
    """Relaunch a program that has been down for two checks in a row.
    At most 3 attempts per 30 minutes, so a broken program can't loop forever."""
    name = prog["name"]
    if status["running"] or not cfg["auto_restart"] or cfg.get("closed"):
        _down_cycles[name] = 0
        return
    _down_cycles[name] = _down_cycles.get(name, 0) + 1
    if _down_cycles[name] < 2:
        return
    cmd, cwd, how = resolve_restart(prog)
    if not cmd:
        return
    now = time.time()
    recent = [t for t in _restart_times.get(name, []) if now - t < 1800]
    if len(recent) >= 3:
        status["note"] = "Not running (restart limit reached)"
        return
    try:
        launch(cmd, cwd)
        recent.append(now)
        _restart_times[name] = recent
        _down_cycles[name] = 0
        status["note"] = "Restarting automatically"
        log.info("Restarted %s using %s", name, how)
    except Exception as e:
        log.info("Restart of %s failed: %s", name, e)


# ───────────────────────── closed screen ─────────────────────────
#
# A full-screen message that covers the booth. It runs as its own small process
# (this same file with --closed-screen) so it survives agent restarts, and it
# comes back after a reboot because the "closed" setting lives on the dashboard.

def read_closed_message():
    try:
        text = CLOSED_MESSAGE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or DEFAULT_CLOSED_MESSAGE


def closed_settings():
    """Optional config.json block:  "closed_screen": {"column": 0.36, "logo": 0.4}
    column = width of the text as a fraction of the screen width (make it smaller if text is cut off)
    logo   = logo width as a fraction of that column, 0 hides the logo."""
    try:
        s = load_config().get("closed_screen") or {}
    except Exception:
        s = {}
    def num(key, default, lo, hi):
        v = s.get(key, default)
        return min(hi, max(lo, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else default
    return {"column": num("column", 0.36, 0.15, 0.95), "logo": num("logo", 0.4, 0.0, 1.0)}
    # "stop_software": false in the same block keeps the booth software running while closed.


def wrap_text(draw, text, font, max_width):
    """Break text into lines no wider than max_width pixels."""
    lines = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            trial = (line + " " + word).strip()
            if draw.textlength(trial, font=font) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def render_closed_frame(sw, sh, message, settings):
    """Black screen, white logo, bold white text in a narrow centred column."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (sw, sh), "#000000")
    draw = ImageDraw.Draw(img)
    col = int(sw * settings["column"])
    font_bytes = base64.b64decode(CLOSED_FONT_B64)

    logo = None
    if settings["logo"] > 0:
        logo = Image.open(io.BytesIO(base64.b64decode(CLOSED_LOGO_B64))).convert("RGBA")
        lw = int(col * settings["logo"])
        logo = logo.resize((lw, round(lw * logo.height / logo.width)), Image.LANCZOS)
    gap = int(sh * 0.06)

    size = max(24, sh // 14)
    while True:                              # shrink until text and logo fit the screen height
        font = ImageFont.truetype(io.BytesIO(font_bytes), size)
        lines = wrap_text(draw, message, font, col)
        line_h = int(size * 1.25)
        text_h = line_h * len(lines)
        total = text_h + ((logo.height + gap) if logo else 0)
        if total <= sh * 0.9 or size <= 20:
            break
        size -= 2

    y = (sh - total) // 2
    if logo:
        img.paste(logo, ((sw - logo.width) // 2, y), logo)
        y += logo.height + gap
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((sw - w) / 2, y), line, font=font, fill="#FFFFFF")
        y += line_h
    return img


def run_closed_screen(auto_close_seconds=None):
    import tkinter as tk
    root = tk.Tk()
    root.title("Closed")
    bg, fg = "#000000", "#FFFFFF"
    root.configure(bg=bg, cursor="none")
    root.overrideredirect(True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    root.attributes("-topmost", True)
    settings = closed_settings()
    message = read_closed_message()
    label = tk.Label(root, bg=bg, fg=fg, borderwidth=0, highlightthickness=0, padx=0, pady=0)
    photo = {}

    def draw_message():
        try:
            from PIL import ImageTk
            photo["img"] = ImageTk.PhotoImage(render_closed_frame(sw, sh, message, settings))
            label.config(image=photo["img"], text="")
            label.place_forget()
            label.place(x=0, y=0, width=sw, height=sh)
        except Exception:                    # no Pillow/ImageTk: plain narrow text instead
            label.config(image="", text=message, justify="center",
                         font=("Segoe UI", max(24, sh // 16), "bold"), wraplength=int(sw * settings["column"]))
            label.place_forget()
            label.place(relx=0.5, rely=0.5, anchor="center")

    draw_message()

    def tick():
        nonlocal message
        new = read_closed_message()
        if new != message:
            message = new
            draw_message()
        root.lift()
        root.attributes("-topmost", True)
        root.after(1000, tick)

    root.after(1000, tick)
    if auto_close_seconds:
        root.after(int(auto_close_seconds * 1000), root.destroy)
    root.mainloop()


def find_overlay_processes():
    mine = os.getpid()
    found = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = p.info.get("cmdline") or []
        if p.info["pid"] != mine and "--closed-screen" in cl and any(str(a).lower().endswith("agent.py") for a in cl):
            found.append(p)
    return found


def tk_available():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


class ClosedScreen:
    """Keeps the closed screen in step with the dashboard setting."""

    def __init__(self):
        self.error = None
        self._look_for_strays = True      # an overlay may have outlived the previous agent

    def sync(self, want, message):
        """Returns True while the closed screen is showing."""
        self.error = None
        if not want and not self._look_for_strays:
            return False
        procs = find_overlay_processes()
        if want:
            text = (message or "").strip() or DEFAULT_CLOSED_MESSAGE
            try:
                if read_closed_message() != text or not CLOSED_MESSAGE_PATH.exists():
                    CLOSED_MESSAGE_PATH.write_text(text, encoding="utf-8")
            except OSError as e:
                self.error = f"Could not save the message: {e}"
            if not procs:
                if not tk_available():
                    self.error = "Python is missing tkinter, so the closed screen cannot be shown"
                    return False
                flags = CREATE_NO_WINDOW | 0x00000200 if os.name == "nt" else 0
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--closed-screen"],
                                 cwd=str(BASE), creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.5)
                procs = find_overlay_processes()
                if not procs:
                    self.error = "The closed screen did not start"
            self._look_for_strays = True
            return bool(procs)
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        self._look_for_strays = False
        return False


# ───────────────────────── printer and camera ─────────────────────────

# Windows printer status flags (PRINTER_STATUS_*), worded for a person at the dashboard.
PRINTER_FLAGS = {
    0x00000001: "Paused",
    0x00000002: "Printer error",
    0x00000004: "Being deleted",
    0x00000008: "Paper jam",
    0x00000010: "Out of paper",
    0x00000020: "Waiting for manual feed",
    0x00000040: "Paper problem",
    0x00000080: "Printer offline",
    0x00000800: "Output tray full",
    0x00001000: "Printer not available",
    0x00040000: "Out of ribbon or toner",
    0x00100000: "Needs attention",
    0x00200000: "Printer out of memory",
    0x00400000: "Door open",
}
# Flags that are normal while printing, shown only in the diagnostics.
PRINTER_INFO_FLAGS = {
    0x00000100: "IO active", 0x00000200: "Busy", 0x00000400: "Printing", 0x00002000: "Waiting",
    0x00004000: "Processing", 0x00008000: "Initialising", 0x00010000: "Warming up",
    0x00020000: "Toner low", 0x01000000: "Power save",
}
JOB_FLAGS = {
    0x00000002: "Print job error",
    0x00000020: "Printer offline",
    0x00000040: "Out of paper",
    0x00000200: "Job blocked",
    0x00000400: "Needs attention",
}
PRINTER_ATTR_WORK_OFFLINE = 0x00000400
STUCK_JOB_SECONDS = 120          # a photo strip prints in well under a minute, so a job older than this is stuck
VIRTUAL_PRINTER_WORDS = ("pdf", "xps", "onenote", "fax", "microsoft print", "send to", "snagit")


def flag_names(value, table):
    return [name for bit, name in table.items() if value & bit]


def printer_problems(status, attributes=0, jobs=()):
    """Turn what Windows says about a printer into a short list of problems.
    jobs: [{"status": int, "age": seconds the job has been waiting}]. Returns [] when all is well."""
    problems = flag_names(status, PRINTER_FLAGS)
    if attributes & PRINTER_ATTR_WORK_OFFLINE and "Printer offline" not in problems:
        problems.append("Printer offline")
    for job in jobs:
        for name in flag_names(job.get("status", 0), JOB_FLAGS):
            if name not in problems:
                problems.append(name)
    if not problems:
        if any(j.get("age", 0) >= STUCK_JOB_SECONDS and not j.get("status", 0) & 0x80 for j in jobs):
            problems.append("Print stuck: check the printer door, paper and ribbon")
    return problems


_chosen_printer = {"name": None}


def pick_printer(printers, wanted=None):
    """printers: [{"name": str, "default": bool}]. Prefer names matching the config, then Mitsubishi
    (CP-D70/D90), then the default printer. Skips PDF/XPS-style virtual printers."""
    real = [p for p in printers if not any(w in p["name"].lower() for w in VIRTUAL_PRINTER_WORDS)]
    if not wanted:
        for p in real:                      # keep watching the queue chosen earlier
            if p["name"] == _chosen_printer["name"]:
                return p
        # Windows can keep a spare queue for the same printer with "use printer offline" ticked.
        # Prefer a queue that is not set that way, so the spare is not mistaken for a fault.
        live = [p for p in real if not p.get("work_offline")]
        if live:
            real = live
    for needles in ([n.lower() for n in (wanted or [])], ["mitsubishi", "cp-d", "cpd"]):
        if needles:
            for p in real:
                if any(n in p["name"].lower() for n in needles):
                    return p
    for p in real:
        if p.get("default"):
            return p
    return real[0] if real else None


def read_printer(wanted=None):
    """Read the booth printer from the Windows print spooler. Returns None if there is no printer."""
    if win32print is None:
        return None
    default = ""
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        pass
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    listing = [{"name": p["pPrinterName"], "default": p["pPrinterName"] == default,
                "work_offline": bool(p.get("Attributes", 0) & PRINTER_ATTR_WORK_OFFLINE)}
               for p in win32print.EnumPrinters(flags, None, 2)]
    chosen = pick_printer(listing, wanted)
    if not chosen:
        return None
    if not wanted:
        _chosen_printer["name"] = chosen["name"]
    handle = win32print.OpenPrinter(chosen["name"])
    try:
        info = win32print.GetPrinter(handle, 2)
        jobs = []
        try:
            # Windows gives the submit time in UTC, so compare against UTC (not local time).
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            for j in win32print.EnumJobs(handle, 0, 50, 1):
                sub = j.get("Submitted")
                age = 0
                if sub:
                    try:
                        age = max(0, (now - sub.replace(tzinfo=None)).total_seconds())
                    except Exception:
                        pass
                jobs.append({"status": j.get("Status", 0), "age": age})
        except Exception:
            pass
    finally:
        win32print.ClosePrinter(handle)
    return {"name": chosen["name"], "status": info.get("Status", 0),
            "attributes": info.get("Attributes", 0), "jobs": jobs}


def check_printer(cfg):
    """One result row for the printer, shaped like a program row."""
    settings = cfg.get("printer") or {}
    if settings.get("enabled") is False or win32print is None:
        return None
    try:
        info = read_printer(settings.get("match"))
    except Exception as e:
        log.info("Printer check failed: %s", e)
        return None
    if info is None:
        return {"name": "Printer", "device": True, "running": True, "ok": False, "note": "No printer found"}
    problems = printer_problems(info["status"], info["attributes"], info["jobs"])
    row = {"name": "Printer", "device": True, "running": True, "ok": not problems,
           "note": "; ".join(problems), "details": {"Printer": info["name"][:60]}}
    if info["jobs"]:
        row["details"]["Queue"] = f"{len(info['jobs'])} job(s) waiting"
    return row


CAMERA_CONNECTED_RE = re.compile(r"connected to\s+(.+)", re.I)


def camera_state(titles):
    """titles: window titles of the camera software. Returns (connected: bool|None, camera name, title)."""
    for t in titles:
        low = t.lower()
        if "not connected" in low or "no camera" in low or "disconnected" in low:
            return False, "", t
    for t in titles:
        m = CAMERA_CONNECTED_RE.search(t)
        if m:
            return True, m.group(1).strip(), t
    for t in titles:
        if t.lower().startswith("dslr remote pro"):
            return False, "", t           # the control window is up but names no camera
    return None, "", ""


def check_camera(cfg, procs):
    """One result row for the camera, read from the camera software's window title.
    Returns None when the camera software is not running (that is reported as its own problem)."""
    settings = cfg.get("camera") or {}
    if settings.get("enabled") is False or not HAVE_WIN32:
        return None
    needles = [n.lower() for n in settings.get("match", ["DSlrRemote"])]
    pids = {p["pid"] for p in procs if any(n in (p["name"] + " " + p["desc"]).lower() for n in needles)}
    if not pids:
        return None
    try:
        titles = [win32gui.GetWindowText(h) for h in top_windows(pids, visible_only=False)]
        titles = [t for t in titles if t]
    except Exception as e:
        log.info("Camera check failed: %s", e)
        return None
    connected, name, raw = camera_state(titles)
    if connected is None:
        return None
    row = {"name": "Camera", "device": True, "running": True, "ok": connected,
           "note": "" if connected else "Camera not connected (check the USB cable and that it is switched on)"}
    if name:
        row["details"] = {"Camera": name[:60]}
    return row


def check_devices(cfg, procs):
    rows = []
    fns = []
    if watching(cfg, "printer"):
        fns.append(lambda: check_printer(cfg))
    if watching(cfg, "camera"):
        fns.append(lambda: check_camera(cfg, procs))
    for fn in fns:
        try:
            row = fn()
        except Exception as e:
            log.info("Device check failed: %s", e)
            row = None
        if row:
            rows.append(row)
    return rows


def show_devices(local_cfg):
    """Diagnostics: show exactly what Windows and the camera software report."""
    print("PRINTER\n")
    if win32print is None:
        print("  pywin32 is missing (win32print).")
    else:
        try:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            for p in win32print.EnumPrinters(flags, None, 2):
                print(f"  {p['pPrinterName']}  status={p['Status']:#x}  attributes={p['Attributes']:#x}")
        except Exception as e:
            print("  Could not list printers:", e)
        info = None
        try:
            info = read_printer((local_cfg.get("printer") or {}).get("match"))
        except Exception as e:
            print("  Could not read the printer:", e)
        if info:
            names = flag_names(info["status"], PRINTER_FLAGS) + flag_names(info["status"], PRINTER_INFO_FLAGS)
            print(f"\n  Watching: {info['name']}")
            print(f"  Windows says: {', '.join(names) or 'ready, nothing to report'}")
            print(f"  Jobs in queue: {len(info['jobs'])}")
            problems = printer_problems(info["status"], info["attributes"], info["jobs"])
            print(f"  BoothWatch would report: {'; '.join(problems) or 'all OK'}")
        elif info is None and win32print is not None:
            print("  No usable printer found.")
    print("\nCAMERA\n")
    procs = scan_processes()
    needles = [n.lower() for n in (local_cfg.get("camera") or {}).get("match", ["DSlrRemote"])]
    pids = {p["pid"] for p in procs if any(n in (p["name"] + " " + p["desc"]).lower() for n in needles)}
    if not pids or not HAVE_WIN32:
        print("  The camera software is not running.")
        return
    titles = [win32gui.GetWindowText(h) for h in top_windows(pids, visible_only=False)]
    for t in titles:
        if t:
            print("  window:", t)
    connected, name, _ = camera_state([t for t in titles if t])
    print(f"\n  BoothWatch would report: {'connected to ' + name if connected else ('camera NOT connected' if connected is False else 'nothing (no matching window)')}")


def watch_printer(local_cfg):
    """Diagnostics: print a line every time the printer's status changes. Open the door, pull the paper,
    unplug it, and see what Windows reports."""
    print("Watching the printer. Open the door, remove the paper, unplug it... Press Ctrl+C to stop.\n")
    last = None
    while True:
        try:
            info = read_printer((local_cfg.get("printer") or {}).get("match"))
        except Exception as e:
            info = None
            line = f"error reading printer: {e}"
        else:
            if info is None:
                line = "no printer found"
            else:
                names = flag_names(info["status"], PRINTER_FLAGS) + flag_names(info["status"], PRINTER_INFO_FLAGS)
                job_names = sorted({n for j in info["jobs"] for n in flag_names(j["status"], JOB_FLAGS)})
                problems = printer_problems(info["status"], info["attributes"], info["jobs"])
                ages = ",".join(f"{int(j['age'])}s/{j['status']:#x}" for j in info["jobs"])
                line = (f"status={info['status']:#010x} attrs={info['attributes']:#x} "
                        f"[{', '.join(names) or 'nothing'}] jobs={len(info['jobs'])}"
                        f"{' (age/status: ' + ages + ')' if ages else ''} {job_names or ''} "
                        f"-> {'; '.join(problems) or 'OK'}")
        if line != last:
            print(time.strftime("%H:%M:%S"), line, flush=True)
            last = line
        time.sleep(1)


# ───────────────────────── paper left, from the printer driver ─────────────────────────

PAGES_RE = re.compile(r"^\s*(\d+)\s*pages?\s*$", re.I)
PRINTER_LABEL_RE = re.compile(r"^\s*the\s+(upper|lower)\s+printer\s*$", re.I)


def parse_remaining_pages(items):
    """Read the 'Remain' box of the Mitsubishi driver's Printing Preferences window.
    items: [(text, top)] where top is the control's height on screen, or plain strings.
    Windows lists controls in a different order from how they look, so labels are matched
    to the 'N pages' value on the same row. Returns {"upper": n, "lower": n, "all": [...], "total": n}."""
    boxes = [(it, None) if isinstance(it, str) else it for it in items]
    pages = []                         # (count, top, index)
    labels = []                        # (which, top, index)
    for i, (t, top) in enumerate(boxes):
        m = PAGES_RE.match(t)
        if m:
            pages.append((int(m.group(1)), top, i))
        lab = PRINTER_LABEL_RE.match(t)
        if lab:
            labels.append((lab.group(1).lower(), top, i))
    found = {}
    used = set()
    for which, ltop, li in labels:
        best = None
        for count, ptop, pi in pages:
            if pi in used:
                continue
            dist = abs(ptop - ltop) + 0.001 * abs(pi - li) if ptop is not None and ltop is not None else abs(pi - li)
            if ptop is not None and ltop is not None and abs(ptop - ltop) > 120:
                continue               # a different row
            if best is None or dist < best[0]:
                best = (dist, count, pi)
        if best:
            found[which] = best[1]
            used.add(best[2])
    found["all"] = [c for c, _, _ in pages]
    found["total"] = sum(found["all"]) if found["all"] else None
    return found


def window_text_boxes(hwnd):
    """[(text, top)] for a window and all its controls, top = vertical position on screen."""
    out = []

    def add(h):
        t = _hwnd_text(h)
        if t:
            try:
                top = win32gui.GetWindowRect(h)[1]
            except Exception:
                top = None
            out.append((t, top))

    add(hwnd)

    def cb(child, _):
        add(child)
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def read_driver_window(printer_name, settle=6, timeout=20, hide=True):
    """Open the printer's Printing Preferences, read its text, close it again. Windows only.
    With hide=True the window is moved off screen straight away and the previously active window
    is made active again, so the booth software is not disturbed.
    Returns [(text, top)], or None if the window never appeared."""
    if not HAVE_WIN32:
        return None
    previous = None
    try:
        previous = win32gui.GetForegroundWindow()
    except Exception:
        pass
    subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", printer_name],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = None
    deadline = time.time() + timeout
    while time.time() < deadline and not hwnd:
        time.sleep(0.1)
        found = []

        def cb(h, _):
            if win32gui.IsWindowVisible(h) and "printing preferences" in win32gui.GetWindowText(h).lower():
                found.append(h)
            return True

        win32gui.EnumWindows(cb, None)
        hwnd = found[0] if found else None
    if not hwnd:
        return None
    try:
        if hide:
            win32gui.SetWindowPos(hwnd, None, -32000, -32000, 0, 0, 0x0001 | 0x0004 | 0x0010)  # no size, no z-order, no activate
            if previous and previous != hwnd:
                win32gui.SetForegroundWindow(previous)
    except Exception:
        pass
    time.sleep(settle)                      # the driver asks the printer, which takes a few seconds
    boxes = window_text_boxes(hwnd)
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.3)
        if hide and previous:
            win32gui.SetForegroundWindow(previous)
    except Exception:
        pass
    return boxes


def read_paper_left(local_cfg):
    """Pages left on the roll, as the printer reports them. None if it could not be read."""
    info = read_printer((local_cfg.get("printer") or {}).get("match"))
    if not info:
        return None
    boxes = read_driver_window(info["name"], hide=True)
    if not boxes:
        return None
    total = parse_remaining_pages(boxes).get("total")
    return total if isinstance(total, int) and 0 <= total <= 5000 else None


PAPER_READ_SECONDS = 20 * 60
_forced_paper = {"value": None}      # a reading taken by the dashboard button, sent with the next report


_screen_seen = {"sig": None, "since": time.time()}


def booth_idle(results, closed_shown):
    """True when nobody is in the middle of a session: closed, on the welcome screen, or the booth has
    sat on the same screen for 3 minutes (a real session keeps changing screens)."""
    sig = "|".join(str(r.get("screen", "")) for r in results)
    if sig != _screen_seen["sig"]:
        _screen_seen["sig"] = sig
        _screen_seen["since"] = time.time()
    if closed_shown:
        return True
    if any(str(r.get("screen", "")).lower() == "welcome" for r in results):
        return True
    return time.time() - _screen_seen["since"] >= 180


def show_paper_left(local_cfg):
    """Diagnostics: the same quiet read the agent does every 20 minutes."""
    print("Reading paper left quietly (about 8 seconds)...")
    print("Pages left according to the printer:", read_paper_left(local_cfg))


def show_printer_info(local_cfg):
    """Diagnostics: look for the paper-left figure the printer driver shows."""
    info = read_printer((local_cfg.get("printer") or {}).get("match"))
    if not info:
        print("No printer found.")
        return
    name = info["name"]
    print(f"Printer: {name}\n")
    print("1. What Windows knows about the printer (driver properties):\n")
    ps = (f"$n='{name}'.Replace(\"'\",\"''\"); "
          "Get-PrinterProperty -PrinterName $n | Format-Table -AutoSize | Out-String -Width 220")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=60).stdout.strip()
        print(out[:6000] or "  (nothing)")
    except Exception as e:
        print("  Could not run PowerShell:", e)
    print("\n2. Opening Printing Preferences for a few seconds and reading it. Please do not touch anything.\n")
    texts = read_driver_window(name, hide=False)
    if texts is None:
        print("  The Printing Preferences window did not appear.")
        return
    for t, top in texts:
        print("  text:", t)
    parsed = parse_remaining_pages(texts)
    print("\n  BoothWatch read:", parsed)



# ───────────────────────── main loop ─────────────────────────

def adjust_for_closed(cfg, prog, status):
    """While the booth is closed on purpose, the booth software is off and the card reader is
    disabled by it. Those are expected, so they must not raise alerts."""
    if not cfg.get("closed") or status.get("ok"):
        return
    if prog.get("stop_when_closed") and cfg.get("stop_software", True):
        if not status.get("running"):
            status.update(ok=True, note="Closed on purpose")
    elif status.get("running") and status.get("note") != "Not responding":
        status.update(ok=True, note="Closed: " + (status.get("note") or "disabled"))


def software_to_stop(cfg):
    return [p for p in cfg.get("programs", []) if p.get("stop_when_closed")] if cfg.get("stop_software", True) else []


def stop_booth_software(cfg):
    """Close the booth software while closed. Returns the names it closed."""
    closed = []
    for prog in software_to_stop(cfg):
        try:
            if stop_program(prog):
                closed.append(prog["name"])
        except Exception as e:
            log.info("Could not close %s: %s", prog.get("name"), e)
    if closed:
        log.info("Booth closed: closed %s so the card machine switches off", ", ".join(closed))
    return closed


def start_booth_software(cfg):
    """Start the booth software again when reopening. Returns True if something was started."""
    started = False
    for prog in software_to_stop(cfg):
        if find_matches(prog, scan_processes()):
            continue
        cmd, cwd, how = resolve_restart(prog)
        if not cmd:
            log.info("Reopening: do not know how to start %s", prog.get("name"))
            continue
        try:
            launch(cmd, cwd)
            started = True
            log.info("Reopening: started %s using %s", prog["name"], how)
        except Exception as e:
            log.info("Reopening: could not start %s: %s", prog.get("name"), e)
    return started


def booth_software_ready(cfg):
    procs = scan_processes()
    for prog in software_to_stop(cfg):
        st = check_program(prog, procs)
        if not st.get("running") or st.get("fullscreen") is False:
            return False
    return True


def run_checks(cfg):
    procs = scan_processes()
    results = []
    for prog in cfg["programs"]:
        status = check_program(prog, procs)
        maybe_restart(cfg, prog, status)
        adjust_for_closed(cfg, prog, status)
        results.append(status)
    results.extend(check_devices(cfg, procs))
    return results


def discover():
    if not HAVE_WIN32:
        print("Discovery needs Windows (pywin32).")
        return
    print("Programs with a visible window (use these names in config.json 'match'):\n")
    seen = set()
    for p in scan_processes():
        windows = top_windows({p["pid"]})
        if not windows or p["name"] in seen:
            continue
        seen.add(p["name"])
        title = win32gui.GetWindowText(windows[0])
        print(f"  exe: {p['name']}\n  description: {p['desc']}\n  path: {p['exe']}\n  window: {title}\n")


def handle_screenshot_request(local_cfg, state, requested_at):
    """Take and upload a screenshot once per dashboard request."""
    state["last_shot"] = requested_at
    save_state(state)
    try:
        image = take_screenshot_b64()
        rpc(local_cfg, "submit_screenshot", {"p_token": state["agent_token"], "p_image": image})
        log.info("Screenshot sent (%d KB)", len(image) // 1024)
    except Exception as e:
        log.info("Screenshot failed: %s", e)


def handle_command(local_cfg, cfg, state, cmd):
    """Carry out one button pressed on the dashboard, then report how it went."""
    cid, kind = cmd.get("id"), cmd.get("kind")
    args = cmd.get("args") or {}
    log.info("Dashboard command %s: %s %s", cid, kind, args)
    ok, text, shutting_down = False, "", False
    try:
        if kind == "restart_program":
            ok, text = restart_program(cfg, str(args.get("program", "")))
        elif kind == "read_paper":
            started = time.time()
            pages = read_paper_left(local_cfg)
            if pages is None:
                ok, text = False, "Could not read the paper figure from the printer driver"
            else:
                _forced_paper["value"] = pages
                ok, text = True, f"Printer reports {pages} pages left (read in {time.time() - started:.0f}s)"
        elif kind == "restart_pc":
            run_restart_pc()
            ok, text, shutting_down = True, "Restarting in 15 seconds", True
        else:
            text = "This agent does not know that command"
    except Exception as e:
        ok, text = False, f"Failed: {e}"
    log.info("Command %s result: %s %s", cid, "ok" if ok else "failed", text)
    try:
        rpc(local_cfg, "complete_command",
            {"p_token": state["agent_token"], "p_id": cid, "p_ok": ok, "p_result": text})
    except Exception as e:
        log.info("Could not report command result: %s", e)
    return shutting_down


def split_private(results):
    """Take the agent-only fields out of the check results before they are sent.
    Returns (print counter or None, photo folder or None)."""
    counter, root = None, None
    for r in results:
        c = r.pop("_print_counter", None)
        f = r.pop("_images_root", None)
        if c is not None:
            counter = c
        if f:
            root = f
    return counter, root


def load_remote_cache():
    try:
        data = json.loads(REMOTE_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_remote_cache(remote_cfg):
    try:
        REMOTE_CACHE_PATH.write_text(json.dumps(remote_cfg), encoding="utf-8")
    except OSError:
        pass


def show_stats(local_cfg, state):
    """Diagnostic: what can this PC read for counting sessions and prints?"""
    results = run_checks(local_cfg)
    counter, root = split_private(results)
    if root:
        state["images_root"] = root
    folder = resolve_images_root(local_cfg, state)
    today = datetime.date.today().isoformat()
    print(f"Print counter read from the booth window: {counter if counter is not None else 'NOT FOUND'}")
    print(f"Photo folder: {folder}")
    n = count_sessions(folder, today)
    if n is None:
        print(f"Folder {Path(folder) / today / 'prints'} was not found (normal before today's first session).")
    else:
        print(f"Sessions today (pictures in {today}\\prints): {n}")


def show_startup():
    entries = startup_entries()
    if not entries:
        print("No Windows startup entries found (or not running on Windows).")
    for e in entries:
        print(f"- {e['source']}\n    {e['match_text'][:160]}")
    print()
    try:
        cfg = load_config()
    except Exception as e:
        print(f"Could not read config.json: {e}")
        return
    for prog in cfg["programs"]:
        cmd, _cwd, how = resolve_restart(prog)
        print(f"{prog['name']}: " + (f"would restart using {how}\n    {cmd}" if cmd else "NO WAY to restart it found"))


# ───────────────────────── outage log ─────────────────────────
# When the booth comes back after an unplanned gap, work out when it went quiet and why, and tell the
# dashboard. Gaps that fall outside opening hours (or while the booth was closed on the dashboard) are
# planned and are not reported.
OUTAGE_MIN_SECONDS = 180      # shorter gaps are ordinary hiccups
SUSPEND_GAP_SECONDS = 240     # the agent loop did not run for this long while the program stayed open = PC was asleep
RUNTIME_SAVE_SECONDS = 30
_WIN_UPDATE_RE = re.compile(r"wuauclt|mousocore|tiworker|trustedinstaller|usoclient|musnotification|windows update|sihclient", re.I)
_EVT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}


def read_windows_events(since_utc):
    """Shutdown, restart, crash and sleep events from the Windows System log, oldest first.
    Returns [] when the log cannot be read (not Windows, no permission)."""
    if sys.platform != "win32":
        return []
    query = ("*[System[(EventID=41 or EventID=42 or EventID=1074 or EventID=6005 or EventID=6006 or EventID=6008)"
             " and TimeCreated[@SystemTime>='%s']]]" % since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    try:
        out = subprocess.run(["wevtutil", "qe", "System", "/q:" + query, "/c:60", "/rd:true", "/f:xml"],
                             capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW).stdout
        return parse_windows_events(out)
    except Exception as e:
        log.info("Could not read the Windows event log: %s", e)
        return []


def parse_windows_events(xml_text):
    import xml.etree.ElementTree as ET
    events = []
    if not xml_text or "<Event" not in xml_text:
        return events
    root = ET.fromstring("<Events>" + re.sub(r"<\?xml[^>]*\?>", "", xml_text) + "</Events>")
    for ev in root.findall("e:Event", _EVT_NS) or root.findall("Event"):
        def find(path):
            return ev.find(path, _EVT_NS)
        sysnode = find("e:System")
        eid = int((sysnode.find("e:EventID", _EVT_NS).text or "0").strip())
        stamp = sysnode.find("e:TimeCreated", _EVT_NS).get("SystemTime")
        t = datetime.datetime.fromisoformat(re.sub(r"\.\d+Z?$|Z$", "", stamp) + "+00:00")
        data = {}
        order = []
        ed = find("e:EventData")
        if ed is not None:
            for d in ed.findall("e:Data", _EVT_NS):
                order.append((d.text or "").strip())
                if d.get("Name"):
                    data[d.get("Name")] = (d.text or "").strip()
        events.append({"id": eid, "time": t, "data": data, "list": order})
    events.sort(key=lambda x: x["time"])
    return events


def classify_gap(events, start, end):
    """Most likely reason the PC went quiet between start and end (aware datetimes)."""
    window = [e for e in events if start - datetime.timedelta(minutes=3) <= e["time"] <= end]
    by = lambda i: [e for e in window if e["id"] == i]
    if by(1074):                                         # a shutdown or restart somebody or something asked for
        e = by(1074)[-1]
        lst = e["list"]
        proc = lst[0] if lst else ""
        reason = lst[2] if len(lst) > 2 else ""
        kind = (lst[4] if len(lst) > 4 else "").lower()
        detail = {"process": re.split(r"[\\/]", proc.split(" (")[0])[-1][:80], "reason": reason[:120], "type": kind}
        if _WIN_UPDATE_RE.search(proc + " " + reason):
            return "windows_update", detail
        return ("restart" if "restart" in kind or "reboot" in kind else "shutdown"), detail
    if by(41) or by(6008):
        e = (by(41) or by(6008))[-1]
        bug = str(e["data"].get("BugcheckCode", "0")).strip()
        if by(41) and bug not in ("", "0"):
            return "crash", {"bugcheck": bug}
        return "power_loss", {}
    if by(42):
        return "sleep", {}
    if by(6006):
        return "shutdown", {}
    return "agent_stopped", {}


def probe_network(supabase_url):
    """Quick look at why a request failed: can this PC reach the internet at all, and the dashboard's server?"""
    host = re.sub(r"^https?://", "", supabase_url or "").split("/")[0]
    result = {"t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}

    def tcp(h, port=443):
        try:
            socket.create_connection((h, port), timeout=3).close()
            return True
        except OSError:
            return False
    try:
        socket.gethostbyname(host)
        result["dns"] = True
    except OSError:
        result["dns"] = False
    result["internet"] = tcp("1.1.1.1") or tcp("8.8.8.8")
    result["service"] = bool(result["dns"]) and tcp(host)
    return result


def summarise_probes(probes):
    if not probes:
        return "network", {"kind": "unknown"}
    if all(not p.get("internet") for p in probes):
        return "network", {"kind": "no_internet", "checks": len(probes)}
    if any(p.get("internet") and not p.get("dns") for p in probes):
        return "network", {"kind": "dns", "checks": len(probes)}
    if any(p.get("internet") and not p.get("service") for p in probes):
        return "network", {"kind": "service_unreachable", "checks": len(probes)}
    return "network", {"kind": "intermittent", "checks": len(probes)}


def open_minutes_between(start, end, hours, closed_manual):
    """Minutes of start..end (aware datetimes) when the booth was meant to be open."""
    if closed_manual:
        return 0
    span = (end - start).total_seconds()
    step = max(60, int(span // 1500) + 1)
    n = 0
    t = start
    while t < end:
        try:
            if in_opening_hours(hours, t.astimezone()):
                n += step
        except Exception:
            n += step
        t += datetime.timedelta(seconds=step)
    return int(min(n, span) // 60)


class OutageTracker:
    def __init__(self):
        self.rt = {}
        try:
            if RUNTIME_PATH.exists():
                self.rt = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.rt = {}
        self.pending = list(self.rt.get("pending") or [])
        self.restart_gap = None
        self.probes = []
        self.last_probe = 0.0
        self.fail_since = None
        self.last_error = None
        self.last_tick = time.time()
        self.suspend = None
        self.last_save = 0.0
        self.last_ok = self.rt.get("last_ok_beat")          # epoch seconds of the last report the dashboard received
        prev_alive = self.rt.get("last_alive")
        now = time.time()
        if prev_alive and self.last_ok and now - prev_alive > OUTAGE_MIN_SECONDS:
            start = datetime.datetime.fromtimestamp(self.last_ok, datetime.timezone.utc)
            events = read_windows_events(start - datetime.timedelta(minutes=5))
            cause, detail = classify_gap(events, datetime.datetime.fromtimestamp(prev_alive, datetime.timezone.utc),
                                         datetime.datetime.now(datetime.timezone.utc))
            self.restart_gap = {"start": self.last_ok, "cause": cause, "detail": detail,
                                "hours": self.rt.get("hours"), "closed_manual": bool(self.rt.get("closed_manual"))}
            log.info("Agent started after a gap of %d min; most likely cause: %s", (now - self.last_ok) / 60, cause)
        elif prev_alive and now - prev_alive > 60:
            log.info("Agent restarted after %d seconds (too short to count as an outage)", now - prev_alive)

    def save(self, remote_cfg=None, force=False):
        now = time.time()
        if not force and now - self.last_save < RUNTIME_SAVE_SECONDS:
            return
        self.last_save = now
        if isinstance(remote_cfg, dict):
            self.rt["hours"] = remote_cfg.get("hours")
            self.rt["closed_manual"] = remote_cfg.get("closed") is True
        self.rt["last_alive"] = now
        self.rt["last_ok_beat"] = self.last_ok
        self.rt["pending"] = self.pending
        try:
            RUNTIME_PATH.write_text(json.dumps(self.rt), encoding="utf-8")
        except Exception as e:
            log.info("Could not save runtime state: %s", e)

    def tick(self, remote_cfg):
        """Call every loop. Notices when the whole PC was frozen or asleep."""
        now = time.time()
        gap = now - self.last_tick
        if gap > SUSPEND_GAP_SECONDS:
            self.suspend = {"start": self.last_tick, "end": now}
            log.info("The PC was asleep or frozen for about %d minutes", gap / 60)
        self.last_tick = now
        self.save(remote_cfg)

    def failed(self, error, supabase_url):
        if self.fail_since is None:
            self.fail_since = time.time()
        self.last_error = str(error)[:160]
        if time.time() - self.last_probe >= 60 and len(self.probes) < 40:
            self.last_probe = time.time()
            try:
                self.probes.append(probe_network(supabase_url))
            except Exception:
                pass

    def succeeded(self, remote_cfg, interval=60):
        """Call after a report the dashboard accepted. Turns any gap since the last one into an outage entry."""
        now = time.time()
        if self.last_ok is not None or self.restart_gap:
            gap_start = (self.restart_gap or {}).get("start") or self.last_ok
            gap = now - gap_start if gap_start else 0
            threshold = max(OUTAGE_MIN_SECONDS, 3 * interval)
            if gap > threshold:
                if self.restart_gap:
                    cause, detail = self.restart_gap["cause"], self.restart_gap["detail"]
                    hours, closed_manual = self.restart_gap["hours"], self.restart_gap["closed_manual"]
                elif self.suspend and self.suspend["end"] >= gap_start:
                    cause, detail = "sleep", {}
                    hours, closed_manual = self.rt.get("hours"), bool(self.rt.get("closed_manual"))
                else:
                    cause, detail = summarise_probes(self.probes)
                    if self.last_error:
                        detail["last_error"] = self.last_error
                    hours, closed_manual = self.rt.get("hours"), bool(self.rt.get("closed_manual"))
                start = datetime.datetime.fromtimestamp(gap_start, datetime.timezone.utc)
                end = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
                open_min = open_minutes_between(start, end, hours, closed_manual)
                if open_min >= 3:
                    self.pending.append({"started_at": start.isoformat(timespec="seconds"), "ended_at": end.isoformat(timespec="seconds"),
                                         "cause": cause, "detail": detail, "open_minutes": open_min})
                    log.info("Offline for %d min (%d of them in opening hours); most likely: %s", gap / 60, open_min, cause)
                else:
                    log.info("Offline for %d min, but while the booth was closed - not logged", gap / 60)
        self.restart_gap = None
        self.suspend = None
        self.fail_since = None
        self.last_error = None
        self.probes = []
        self.last_ok = now
        self.save(remote_cfg, force=True)

    def flush(self, local_cfg, token):
        keep = []
        for o in self.pending:
            try:
                rpc(local_cfg, "report_outage", {"p_token": token, "p_started_at": o["started_at"], "p_ended_at": o["ended_at"],
                                                 "p_cause": o["cause"], "p_detail": o["detail"], "p_open_minutes": o["open_minutes"]})
            except Exception as e:
                log.info("Could not send outage report yet: %s", e)
                keep.append(o)
        if len(keep) != len(self.pending):
            self.pending = keep
            self.save(force=True)


# ───────────────────────── event timeline ─────────────────────────
# Notes what happens on the booth (each session, prints, software or device going up or down, PC boot,
# booth closed/opened) so the dashboard can show a full log, last session time and busiest hours.
EVENT_KEEP = 2000

def session_times(images_root, day):
    """When each session picture for the day was saved (epoch seconds, oldest first)."""
    folder = Path(images_root) / day / "prints"
    try:
        return sorted(f.stat().st_mtime for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    except OSError:
        return []

def pc_boot_epoch():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - k.GetTickCount64() / 1000.0
    except Exception:
        return None

def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

class EventRecorder:
    def __init__(self):
        self.st = {}
        try:
            if EVENTS_PATH.exists():
                self.st = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.st = {}
        self.queue = list(self.st.get("queue") or [])
        self.last_session = self.st.get("last_session")      # newest session picture time already recorded
        self.counter = None
        self.prog = {}
        self.closed = None
        self.dirty = False
        now = time.time()
        boot = pc_boot_epoch()
        if boot and abs(boot - (self.st.get("boot") or 0)) > 120:
            self.add("pc_boot", boot)
            self.st["boot"] = boot
        if self.st.get("version") != VERSION:
            self.add("agent_update" if self.st.get("version") else "agent_start", now, detail={"version": VERSION, "from": self.st.get("version")})
            self.st["version"] = VERSION
        elif now - (self.st.get("alive") or 0) > 180:
            self.add("agent_start", now, detail={"version": VERSION})
        self.save()

    def add(self, kind, t=None, n=1, detail=None):
        ev = {"at": _iso(time.time() if t is None else t), "kind": kind, "n": n, "detail": detail or {}}
        self.queue.append(ev)
        self.queue = self.queue[-EVENT_KEEP:]
        self.dirty = True

    def save(self):
        self.st["queue"] = self.queue
        self.st["last_session"] = self.last_session
        self.st["alive"] = time.time()
        try:
            EVENTS_PATH.write_text(json.dumps(self.st), encoding="utf-8")
        except Exception as e:
            log.info("Could not save event timeline: %s", e)

    def observe(self, results, counter, images_root, today, closed):
        try:
            # sessions: pictures newer than the last one recorded (the first time, all of today's)
            if images_root:
                times = session_times(images_root, today)
                fresh = [t for t in times if self.last_session is None or t > self.last_session + 0.001]
                for t in fresh[-200:]:
                    self.add("session", t)
                if times:
                    self.last_session = max(times[-1], self.last_session or 0)
                elif self.last_session is None:
                    self.last_session = time.time() - 1
            # prints: the booth software's counter went up
            if counter is not None:
                if self.counter is not None and counter > self.counter:
                    self.add("print", n=int(counter - self.counter))
                self.counter = counter
            # software and devices: only changes
            for r in results or []:
                name = r.get("name")
                if not name:
                    continue
                ok = bool(r.get("ok"))
                if name in self.prog and self.prog[name] != ok:
                    self.add("device" if r.get("device") else "software", detail={"name": name, "ok": ok, "note": r.get("note") or ""})
                self.prog[name] = ok
            if self.closed is not None and self.closed != bool(closed):
                self.add("closed" if closed else "opened")
            self.closed = bool(closed)
            if self.dirty:
                self.save(); self.dirty = False
        except Exception as e:
            log.info("Event timeline problem: %s", e)

    def flush(self, local_cfg, token):
        if not self.queue:
            return
        batch = self.queue[:250]
        try:
            rpc(local_cfg, "report_events", {"p_token": token, "p_events": batch})
            self.queue = self.queue[len(batch):]
            self.save()
        except Exception as e:
            log.info("Could not send event timeline yet: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--pair", action="store_true")
    ap.add_argument("--pair-only", action="store_true")   # used by the installer: make a code, then exit
    ap.add_argument("--screenshot", action="store_true")
    ap.add_argument("--closed-screen", action="store_true")
    ap.add_argument("--test-closed", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--startup", action="store_true")
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--watch-printer", action="store_true")
    ap.add_argument("--printer-info", action="store_true")
    ap.add_argument("--paper-left", action="store_true")
    args = ap.parse_args()

    make_dpi_aware()
    if args.closed_screen:          # the overlay process: no logging, no checks
        run_closed_screen()
        return
    if args.test_closed:
        if not tk_available():
            print("Python is missing tkinter. Re-run the Python installer, choose Modify, and tick 'tcl/tk and IDLE'.")
            return
        print("Showing the closed screen for 8 seconds...")
        run_closed_screen(auto_close_seconds=8)
        return

    setup_logging()
    local_cfg = load_config()

    if args.discover:
        discover()
        return
    if args.once:
        print(json.dumps(run_checks(local_cfg), indent=2))
        return
    if args.stats:
        show_stats(local_cfg, load_state())
        return
    if args.startup:
        show_startup()
        return
    if args.devices:
        show_devices(local_cfg)
        return
    if args.paper_left:
        show_paper_left(local_cfg)
        return
    if args.printer_info:
        show_printer_info(local_cfg)
        return
    if args.watch_printer:
        try:
            watch_printer(local_cfg)
        except KeyboardInterrupt:
            pass
        return
    if args.screenshot:
        Path("screenshot-test.jpg").write_bytes(base64.b64decode(take_screenshot_b64()))
        print("Saved screenshot-test.jpg")
        return

    state = load_state()
    if args.pair_only:
        if "agent_token" not in state:
            pair(local_cfg, state)
        return
    if args.pair or "agent_token" not in state:
        pair(local_cfg, state)
        if sys.stdout and sys.stdout.isatty():
            input("Press Enter once you have written the code down...")

    log.info("BoothWatch agent %s started", VERSION)
    remote_cfg = load_remote_cache()
    closed_screen = ClosedScreen()
    closed_shown = False
    was_closed = False          # booth software has been closed for this closure
    reopen_until = None         # while the software starts again, the closed screen stays up
    last_restart_cmd = 0.0
    paper_state = {"last": time.time() - PAPER_READ_SECONDS + 90}   # first read about 90 seconds after start
    last_beat = 0.0
    last_poll_error = None
    print_matcher = PrintMatcher()
    outages = OutageTracker()
    timeline = EventRecorder()

    while True:
        outages.tick(remote_cfg)
        # 1. Ask the dashboard for settings, screenshot requests and button presses (every ~10s)
        commands = []
        try:
            poll = rpc(local_cfg, "agent_poll", {"p_token": state["agent_token"]})
            new_remote = poll.get("config") or {}
            if new_remote != remote_cfg:
                remote_cfg = new_remote
                save_remote_cache(remote_cfg)
            requested = poll.get("screenshot_requested_at")
            if requested and requested != state.get("last_shot"):
                handle_screenshot_request(local_cfg, state, requested)
            commands = poll.get("commands") or []
            last_poll_error = None
        except Exception as e:
            outages.failed(e, local_cfg.get("supabase_url"))
            if str(e) != last_poll_error:       # don't repeat the same error every 10s
                log.info("Settings check failed (using saved settings): %s", e)
                last_poll_error = str(e)

        cfg = effective_config(local_cfg, remote_cfg)

        # 2. Dashboard buttons
        for cmd in commands:
            if handle_command(local_cfg, cfg, state, cmd):
                break                           # the PC is restarting
            last_beat = 0.0                     # report the new state straight away

        # 3. Closed screen follows the dashboard setting. While closed, the booth software is closed too
        #    so the card machine is switched off. Reopening starts it again before the screen goes.
        try:
            want_closed = bool(cfg.get("closed"))
            message = remote_cfg.get("closed_message")
            if any(c.get("kind") == "restart_program" for c in commands):
                last_restart_cmd = time.time()
            if want_closed:
                reopen_until = None
                closed_shown = closed_screen.sync(True, message)
                # Keep it closed (also after a reboot), but leave it alone just after a Restart button.
                if time.time() - last_restart_cmd > 120:
                    stop_booth_software(cfg)
                was_closed = True
            elif was_closed:
                was_closed = False
                if start_booth_software(cfg):
                    reopen_until = time.time() + 75
                closed_shown = closed_screen.sync(reopen_until is not None, message)
            elif reopen_until is not None:
                if booth_software_ready(cfg) or time.time() > reopen_until:
                    time.sleep(3)
                    reopen_until = None
                    closed_shown = closed_screen.sync(False, message)
                else:
                    closed_shown = closed_screen.sync(True, message)
            else:
                closed_shown = closed_screen.sync(False, message)
        except Exception as e:
            closed_screen.error = f"Closed screen problem: {e}"
            log.info("%s", closed_screen.error)

        # 4. Full check + report (every interval_seconds)
        if time.time() - last_beat >= max(15, int(cfg["interval_seconds"])):
            try:
                results = run_checks(cfg)
                counter, root = split_private(results)
                if root and state.get("images_root") != root:
                    state["images_root"] = root
                    save_state(state)
                rpc(local_cfg, "report_heartbeat", {
                    "p_token": state["agent_token"],
                    "p_programs": results,
                    "p_agent_version": VERSION,
                })
                last_beat = time.time()
                outages.succeeded(remote_cfg, int(cfg["interval_seconds"]))
                outages.flush(local_cfg, state["agent_token"])
                bad = [r for r in results if not r["ok"]]
                if bad:
                    log.info("Problems: %s", "; ".join(f"{r['name']}: {r['note']}" for r in bad))

                paper = _forced_paper["value"]
                _forced_paper["value"] = None
                idle_now = booth_idle(results, closed_shown)      # call every beat so it can track screen changes
                # 0.9.1: paper left just ticks down with each print on the server. The printer is only asked when the
                # dashboard's "Read paper now" button is pressed (or if config.json has printer.read_paper_left = true).
                auto_paper = (local_cfg.get("printer") or {}).get("read_paper_left", False) is True
                due = auto_paper and time.time() - paper_state["last"] >= PAPER_READ_SECONDS
                if due and not idle_now and time.time() - paper_state.get("warned", 0) > 600:
                    paper_state["warned"] = time.time()
                    log.info("Paper read is due but the booth looks busy, waiting for a quiet moment")
                if paper is None and ((local_cfg.get("printer") or {}).get("read_paper_left", False) is True
                        and watching(cfg, "printer") and due and idle_now):
                    paper_state["last"] = time.time()
                    try:
                        paper = read_paper_left(local_cfg)
                        log.info("Printer reports %s pages left", paper)
                    except Exception as e:
                        log.info("Could not read pages left: %s", e)

                folder = resolve_images_root(local_cfg, state)
                today = datetime.date.today()
                sessions = count_sessions(folder, today.isoformat())
                pm_cfg = (cfg.get("alerts") or {}).get("print_missing") or {}
                try:
                    pm_grace = float(pm_cfg.get("delay", 5))
                except (TypeError, ValueError):
                    pm_grace = 5
                missing = print_matcher.update(today.isoformat(), sessions, counter, pm_grace)
                if closed_shown or not watching(cfg, "printer"):
                    missing = 0
                timeline.observe(results, counter, folder, today.isoformat(), bool(cfg.get("closed")) or bool(cfg.get("scheduled_closed")))
                status = {"closed_shown": closed_shown, "folder_found": sessions is not None,
                          "print_missing": missing,
                          "counter_seen": counter is not None,
                          "closed_scheduled": bool(cfg.get("scheduled_closed"))}
                if closed_screen.error:
                    status["closed_error"] = closed_screen.error
                rpc(local_cfg, "report_stats", {
                    "p_token": state["agent_token"],
                    "p_day": today.isoformat(),
                    "p_sessions": sessions or 0,
                    "p_print_counter": counter,
                    "p_status": status,
                    **({"p_paper_left": paper} if paper is not None else {}),
                })
                timeline.flush(local_cfg, state["agent_token"])
            except Exception as e:
                outages.failed(e, local_cfg.get("supabase_url"))
                log.info("Check-in failed (will retry): %s", e)

        time.sleep(POLL_SECONDS)


# ───────────────────────── closed screen assets ─────────────────────────
# Zen Kaku Gothic New Black (SIL Open Font License), Latin letters only, and the Photo Pronto logo in white.
# Stored inside this file so a self-update brings them along.
CLOSED_FONT_B64 = (
    "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"
    "GRchQTwwOxwqIxgcNVEpQGMYTyUoGhYiQTwwPBsqIxkcNlEUHjVDChUKHTRDCxQKMzksHRcWFBgmGBQkMSYhQhEUMx8xPh4wMjQe"
    "FBUUGSYXEyUxJyFBERUyHzE/HgEcEyEdIxwFCQUUIRwjHAQKAAADADz/9gMMAsYAEwAnAEMAAAUiLgI1ND4CMzIeAhUUDgInMj4C"
    "NTQuAiMiDgIVFB4CNyImJjU0NjYzMhYXByYmIyIGFRQWMzI2NxcGBgGkS4JjODhjgktLgmM4OGOCSzpmTSsrTWY6OmZNKytNZj08"
    "XTY2XTw0WCJAIjIaN0NDNxo0JkUkXQo4Y4JLS4JjODhjgktLgmM4UCtNZjo6Zk0rK01mOjpmTSszOGdGR2c5KiYxGhhURENTGBkm"
    "KDIABAA8AFoCqALGABMAIwAxADoAACUiLgI1ND4CMzIeAhUUDgInMjY2NTQmJiMiBgYVFBYWJxEzMhYVFAYHFyMnIxU1MzI2NTQm"
    "IyMBckBwVjAwVnBAQHBWMDBWcEBBaz8/a0FBaz8/az6mMT4mHz5dMi9KERYWEUpaMFZwQEBwVjAwVnBAQHBWMEs/a0FBaz8/a0FB"
    "az88AV4uPjExCIiEhM4QFBMPAAIAGgIYAOsC6gALABcAABMiJjU0NjMyFhUUBicyNjU0JiMiBhUUFoMsPT0sKz09KxcfIBcWICAC"
    "GD4rLD09LCs+MiEWFyAgFxYhAAABAEP/5ACqAtkAAwAAFxEzEUNnHAL1/QsAAAIAQ//kAKoC2QADAAcAABcRMxEDETMRQ2dnZxwB"
    "Mv7OAcMBMv7OAAACADICIAFzAp4ACwAXAAATIiY1NDYzMhYVFAYzIiY1NDYzMhYVFAZxGSYmGRolJakZJiYZGiUlAiAmGRolJRoZ"
    "JiYZGiUlGhkmAAEAMgIQAUsDDwADAAABJzcXASXzS84CEKdY0wABAGICEAF7Aw8AAwAAEyc3F4gmzksCECzTWAAAAQAyAhoBfQLY"
    "AAYAABMzFwcnByexTn4mf4AmAtiSLFpaLAAAAgAoAggBBALkAAwAGAAAEyImNTQ2MzIWFRQGBicyNjU0JiMiBhUUFpYuQEAuLkAe"
    "MR8UHh4UFR0dAghBLS5AQC4eMh48HhQUHh4UFB4AAQAoAkIBewLVABUAABMnNjYzMhYWMzI2NxcGBiMiJiYjIgZjOxA6IBktKBAO"
    "GAo7EDsgGSsmEw4ZAkk8Iy0aGxwROyMtGxodAAABAI3/PwFcAAAADgAAFzUyNjU0IzczBxYWFRQGjUcuXhY/CS89cME8Dg4cTR0G"
    "JiA0JAAAAgAyAjQBcwKyAAsAFwAAEyImNTQ2MzIWFRQGMyImNTQ2MzIWFRQGcRkmJhkaJSWpGSYmGRolJQI0JhkaJSUaGSYmGRol"
    "JRoZJgABADICEAFLAw8AAwAAASc3FwEl80vOAhCnWNMAAQBiAhABewMPAAMAABMnNxeIJs5LAhAs01gAAAEAIAJdAQoCrwADAAAB"
    "IzUzAQrq6gJdUgAAAQCN/z8BXAAAAA4AABc1MjY1NCM3MwcWFhUUBo1HLl4WPwkvPXDBPA4OHE0dBiYgNCQAAAEAAADTAR8AFABJ"
    "AAQAAQAAAAAAAAAAAAAAAAADAAEAAAAAABkAJQAxAD0ASQBVAGEAgwC1AOQA8AEQATkBTwFbAWcBcwF/AZMBxgHdAekB9QIBAg0C"
    "GQI3AlACXwJ5Ao8CmwLNAtkC5QLxAv0DQQNNA3ADlAPQA/cENwRIBGYEcgR+BIoElgSoBMQE3QTyBP4FFAVdBWgFcwV+BYkFlAWf"
    "BgIGLwZYBmMGkAbSBwEHDAcXByIHLQdPB4wHrAfIB9QH3wfqB/UIAAgsCEIITgiACKAIqwjXCOII7Qj4CQMJQglNCXoJpgnTCfEK"
    "Kwp4Cp0KvgrJCtQK3wrqCvwLGAswC1ULYAtrC38LiwuXC8gL2QwLDEoMZQyVDNQM7w03DXYNhw23DfQODQ4bDisOOw5LDmEOhA6q"
    "Dt0PEQ8uD0wPgg+5D88P7BAbECkQORBWEHMQqBDdEO4Q/xELERMRHxErETcRPxF/Eb8R4hIFEh4SOBJNElsSWxJbEpUS2RMlE2wT"
    "txPdE/YUDxQ8FE4UYBRzFJAUoBTHFNoU/hVCFasWAhYeFoQW4xc2F1wXaRd9F6IXsBe+F9AX9xgcGDYYWxhpGHcYhBieAAEAAAAB"
    "AIMrf34UXw889QALA+gAAAAA3MO3EQAAAADd+Qdl/4j/PQQZA+kAAAAGAAIAAAAAAAAD6AAAApEACAKRAAgCkQAIApEACAKRAAgC"
    "kQAIApEACAOWAAgCgABDAqAAIQKgACECoQBDAqEABAJMAEMCTABDAkwAQwJMAEMCTABDAjoAQwKZACECtQBDAQgAQwEIABUBCP/e"
    "AQj/4wEI/9oCFQARAmMAQwI8AEMDBABDAsMAQwLDAEMC1gAhAtYAIQLWACEC1gAhAtYAIQLWACEC1gAhAngAQwJ4AEMC1gAhAoEA"
    "QwJ0ACoCUAAZApYAOgKWADoClgA6ApYAOgKWADoCfwAIAz8AEQJ5ABECfQARAn0AEQI/ACECFgAnAhYAJwIWACcCFgAnAhYAJwIW"
    "ACcCFgAnAzUAJwI3ADoB7AAhAewAIQI3ACECGQAhAggAIQIIACECCAAhAggAIQIIACEBcwARAiMAIQIUADoBDgA6AQ4ASQEOABgB"
    "Dv/hAQ7/5gEO/90BBf+9AgMAOgDyADoDDwA6AhQAOgIUADoCGQAhAhkAIQIZACECGQAhAhkAIQIZACECGQAhAjcAOgI4ADoCNwAh"
    "AX8AOgHOABkCvQARAYMAEQILADICCwAyAgsAMgILADICCwAyAf8AEQKwABECIAARAfkACAH5AAgB+QAIAbEAGQIWACcCGQAhAfYA"
    "KgGgAAABwgAZAeEAEQHuAAgB1wARAfEAKAGhAAgB/AAqAeAAEQE2AAABRQAPAXEAEQFkAAMBRf+IA2oAAANOAAADaAARAQYAMgEx"
    "ADIBBgAyASAAMgNBADoBKQBDASkAQwHtACEB7QAhAQYAMgIJADICVwAhAiAAKgIgACoBlQBQAZUAKAHfADwB3wAoAV4AUAFeACgB"
    "vQA8Ab0APAIUAEED6AAAAskAPAExADICDwAoAg8AKAEdACgBHQAoAmAAKAJgADwB3AA8AQMAPAEYAAABGAAAA0EAqgK6ADUCdAAq"
    "An8AHgNBAKcDQQByA0EAWQNBAHUDQQBcA0EAXAJYADICWAAyA0EAXANBAE8CVQAqAhYAIQIKADIDCQAqAxoAIQKgACEDQQCyA0EA"
    "1ANIADwC5AA8A0EAGgDtAEMA7QBDAAAAMgAAADIAAABiAAAAMgAAACgAAAAoAAAAjQGlADIBfQAyAZMAYgEqACAB9ACNAAEAAASI"
    "/uAAAAPw/4j+gwQZAAEAAAAAAAAAAAAAAAAAAADTAAQD0wOEAAUAAAKKAlgAAABLAooCWAAAAV4AFAEhAAAAAAAAAAAAAAAAgAAA"
    "AwAAAAIAAAAAAAAAAFpFTiEAQAAgIKwDcP+IAAAEiAEgAAAAAQAAAAAB4gK8AAAAIAADAAAAAgAAAAMAAAAUAAMAAQAAABQABAHQ"
    "AAAAFAAQAAMABAAvADkAfgD/IBQgGiAdICYgrP//AAAAIAAwADoAoCATIBggHCAmIKz//wAAAEYAAAAA4IsAAOCG4GbgAwABABQA"
    "AAAwALgAAAF0AAAAAAAAAAAAqgCNAKgAkwCuAL0AvwCpAJYAlwCSALIAiQCcAIgAlACKAIsAtwC1ALYAjwC+AAEACQAKAAwADgAT"
    "ABQAFQAWABsAHAAdAB4AHwAhACgAKgArACwALQAuADMANAA1ADYAOACaAJUAmwC7AKAAzwA5AEEAQgBEAEYASwBMAE0ATgBUAFUA"
    "VgBXAFgAWgBhAGMAZABlAGcAaABtAG4AbwBwAHMAmADFAJkAugCrAI4ArACwAK0AsQDGAMEAzgDCAHQApgC5AJ0AwwDRAMQAuACB"
    "AIIA0AC8AMAAkQDSAIAAdQCnAIYAhQCHAJAABQACAAMABwAEAAYACAALABIADwAQABEAGgAXABgAGQANACAAJQAiACMAJwAkALMA"
    "JgAyAC8AMAAxADcAKQBmAD0AOgA7AD8APAA+AEAAQwBKAEcASABJAFMAUABRAFIARQBZAF4AWwBcAGAAXQC0AF8AbABpAGoAawBx"
    "AGIAcgCkAKUAoQAAAAcAWgADAAEECQAAAKwAAAADAAEECQABADIArAADAAEECQACAA4A3gADAAEECQADAEIA7AADAAEECQAEADIA"
    "rAADAAEECQAFABoBLgADAAEECQAGACwBSABDAG8AcAB5AHIAaQBnAGgAdAAgADIAMAAyADIAIABUAGgAZQAgAFoAZQBuACAAUABy"
    "AG8AagBlAGMAdAAgAEEAdQB0AGgAbwByAHMAIAAoAGgAdAB0AHAAcwA6AC8ALwBnAGkAdABoAHUAYgAuAGMAbwBtAC8AZwBvAG8A"
    "ZwBsAGUAZgBvAG4AdABzAC8AegBlAG4ALQBrAGEAawB1AGcAbwB0AGgAaQBjACkAWgBlAG4AIABLAGEAawB1ACAARwBvAHQAaABp"
    "AGMAIABOAGUAdwAgAEIAbABhAGMAawBSAGUAZwB1AGwAYQByADEALgAwADAAMgA7AFoARQBOACEAOwBaAGUAbgBLAGEAawB1AEcA"
    "bwB0AGgAaQBjAE4AZQB3AC0AQgBsAGEAYwBrAFYAZQByAHMAaQBvAG4AIAAxAC4AMAAwADIAWgBlAG4ASwBhAGsAdQBHAG8AdABo"
    "AGkAYwBOAGUAdwAtAEIAbABhAGMAawAAAAMAAAAAAAD/ewAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAB//8ACgABAAAACABiAF4A"
    "BAAGREZMVAAmY3lybAA4Z3JlawA4aGFuaQAma2FuYQAmbGF0bgA4AAYAAAAAAAIABAAeACIAJgCSAAYAAAAAAAMABAAMABAAFACA"
    "AAH/sgABA0YAAf+IAAQAFgAEaWNmYmljZnRpZGVvcm9tbgAGREZMVAAmY3lybAA4Z3JlawA4aGFuaQAma2FuYQAmbGF0bgA4AAYA"
    "AAAAAAIABAAeACIAJgAqAAYAAAAAAAMABAAMABAAFAAYAAEAJQABA8MAAQAAAAEAeAAAAAEAAgAOAAAAAAAAAIQAAgATAAEAAQAB"
    "AAoACgABAA4ADgABABYAFgABAB8AHwABACEAIQABAC4ALgABADQANAABADYANgABADkAOQABAEIAQgABAEYARgABAFgAWAABAFoA"
    "WgABAGgAaAABAG4AbgABAHAAcAABAM4A0AADANIA0gADAAEAAgAAAAwAAAASAAEAAQDSAAEAAwDOAM8A0AABAAAACgAeACwAAURG"
    "TFQACAAEAAAAAP//AAEAAAABa2VybgAIAAAAAQAAAAEABAACAAgAAgAKAEgAAQAUAAQAAAAFACIAOAA4ADgAOAABAAUAfQCIAIkA"
    "jAChAAUAev/iAIj/xACJ/8QAjP/EAKH/xAABAH3/2AACBXoABAAABcwGzAAhABUAAAAAAAAAAP/2/+wAAAAA/7D/2P/Y/87/4v/s"
    "/8T/4gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/84AAAAAAAAAAP/sAAD/4v/sAAAAAAAAAAAAAAAAAAD/7AAA/+IAAAAA/87/"
    "2AAA/+wAAAAA//YAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/sAAAAAAAAAAAAAAAAAAAAAP/sAAAAAAAA"
    "AAAAAAAA/7oAAP/i/8QAAP/s/8QAAP/iAAAAAAAAAAAAAP/sAAD/4gAAAAAAAAAA/5wAAAAA/7AAAP/E/5z/nP/EAAAAAAAAAAAA"
    "AP/OAAD/7P/O/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/7AAAAAAAAAAAAAAAAAAAAAA/84AAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/YAAAAAAAAAAAAAAAAAAAAAP+w/87/zv+w/5z/xAAAAAAAAAAAAAD/"
    "2P/YAAAAAP/Y/7D/xP/O/3QAAP/YAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/84AAP/sAAAAAAAA/8QAAP/Y"
    "/9j/xAAAAAAAAAAAAAAAAAAA/+wAAAAAAAAAAP/YAAD/2P/sAAD/4gAAAAAAAAAAAAAAAP+6AAAAAP/OAAAAAAAAAAAAAP/iAAAA"
    "AP/iAAAAAAAA/9gAAAAAAAAAAAAAAAAAAP/sAAAAAAAAAAAAAP/sAAAAAAAAAAAAAAAA/8QAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAP/EAAD/9v/E/5wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/4gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/iAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/7oAAAAAAAAAAP/sAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/YAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/EAAAAAP/iAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9gAAAAAAAAAAAAA/+IAAAAA/5wA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/2oAAP/YAAAAAP/sAAAAAAAAAAAAAAAAAAAAAAAA/8QAAAAA/+IAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAA/+wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/iAAAAAAAA/7oAAAAAAAAAAAAAAAD/4gAAAAAAAAAAAAAA"
    "AAAA/7AAAAAAAAAAAAAAAAAAAAAA/87/2AAA/9gAAAAA/+IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+IAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/84AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/8QA"
    "AAAA/9gAAAAA/8QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/YAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/iAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/5IAAAAA/7oAAP+6/5z/agAAAAAAAAAAAAAA"
    "AP/YAAD/zv/iAAAAAAAAAAAAAAAAAAAAAP+6AAAAAP/EAAAAAAAAAAAAAAAAAAAAAAAA/8QAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAA/2oAAgANAAEABwAAAAkADQAHABMAEwAMABwAHQANACEALQAPADMANwAcADkAQwAhAEUATQAsAFUAVQA1AFcAcgA2AIgA"
    "iQBSAIwAjABUAKEApQBVAAIAKgABAAcABQAJAAkAGAAKAAsAEAAMAA0AAgATABMAIAAcABwAHwAdAB0AHgAhACcAAgAoACgAFwAp"
    "ACoAAgArACsAHQAsACwAFgAtAC0ADwAzADMABgA0ADQACgA1ADUADAA2ADcACQA5AD8ABABAAEAAAwBCAEMADgBHAEoAAwBLAEsA"
    "HABMAEwAGwBNAE0AAQBVAFUAFQBXAFkAAQBjAGMAGgBkAGQAEgBlAGYAEQBnAGcAGQBoAGwABwBtAG4ACwBvAG8ADQBwAHAACwBx"
    "AHIABgCIAIkACACMAIwACAChAKEACACiAKIAFACjAKMAEwCkAKQAFAClAKUAEwACACAAAQAIAAUACgALAAMAFAAUAAMAGwAbABQA"
    "IQAnAAMAKgAqAAMALAAsABMALQAtAA4AMwAzAAsANAA0AAoANQA1AAkANgA3AAgAOQBAAAQAQgBKAAEATABMAAEAVwBZAAIAWgBg"
    "AAEAYQBhABIAYwBjAAEAZABkAAIAZQBlABEAZwBnABAAaABsAAYAbQBuAA0AbwBvAAwAcABwAA0AcQByAAsAiACJAAcAjACMAAcA"
    "oQChAAcAowCjAA8ApQClAA8AAQAAAAoAHAAeAAFERkxUAAgABAAAAAD//wAAAAAAAAPoA3AAtP+H/77/+P+H/7L/wQC0ALQAoQCh"
    "ALQAtAC0/4f/vv/4/4cAtAChALQAtP+H/77/+P+HALQAtAC0ALQAtP/BAKH/h/++//j/hwCB/8EAtAC0AKEAtAChALQAtP+H/77/"
    "+P+HALQAtAC0ALT/hwC0AYIAYQCYANIAYQCMAJsBggCpAYIBggCpAHYBggBhAJgA0gBhAJ0BggCpAKsBjgBhAJgA0gBhAKsAqQCp"
    "AYIBggCbAYIAYQCYANIAYQFqAJsBggC0AYIBggGBAJ0A5wGOAGEAmADSAGEBjgGOAY4BjgBhANIBjgBdAF0AqQCzAK4AqQCxALQA"
    "qQC0AKkAqQC3ALMArwC1AJsAmwCbAJsC2gLlAYIBggLZALQApACoAKgBggC0ALQAgACAADEAMQA4ADgAOAA4AjwCPAI8AdsDrQLl"
    "AJ0ApwCdAKcBlQGVAFYAVgNwA3AAbADLAGwAoQCkALQAxQDiAOEBgQF9AX0AvgF9AcEAtAGOAJsApAChAJQAoACqAKoAhgCXAJcA"
    "0gBhAGEAmACMAJsDcAC+AGEAYQDBA3AAARAAAfT+DAAAA+j/h/+1BDMAAAABAAAAAAAAAAAAAAAAAAE="
)
CLOSED_LOGO_B64 = (
    "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"
    "gcAcwf8PFi4H9szY/+wAAAAASUVORK5CYII="
)


if __name__ == "__main__":
    main()
