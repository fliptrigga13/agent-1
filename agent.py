#!/usr/bin/env python3
"""
agent-1: persistent personal agent for Lauren Flipo.
Brain: Gemini 3.8 Flash via API + function calling.
UI: Telegram long-polling (only answers the owner).
Memory: SQLite (facts) + markdown notes.
Safety: destructive-command denylist, non-root user, owner-only chat.
Zero third-party dependencies — stdlib only.

Usage:
  GEMINI_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_ID=... python3 agent.py
  python3 agent.py --once "prompt"   # single turn (for cron), sends result via Telegram
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime

# ---------------- config ----------------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or sys.exit("missing GEMINI_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("missing TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID") or 0) or sys.exit("missing TELEGRAM_OWNER_ID")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
API = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
WORKDIR = os.environ.get("AGENT_WORKDIR", os.path.expanduser("~/workspace"))
DB = os.path.join(WORKDIR, "memory.db")
MAX_STEPS = 10

os.makedirs(WORKDIR, exist_ok=True)

SYSTEM_PROMPT = """You are Agent-1, Lauren Flipo's personal AI agent. You run on your own
virtual machine with a terminal, files, and web access. You are warm, direct,
and terse like he is — no fluff, no preamble. You verify before you claim.

RULES:
- Never delete data, never touch anything outside the workspace, never run a
  destructive command. If unsure, ask first.
- Never expose secrets or keys in chat.
- Lauren's GitHub: fliptrigga13. His name (Lauren Flipo) goes on user-facing builds.
- Save durable facts about him with memory_save so you remember across sessions.
- When a task is done, report what landed and where. Proof, not promises."""

# ---------------- safety ----------------
DENY = [
    r"\brm\s+-rf\s+/(?:\s|$)", r"\bmkfs\b", r"\bdd\b.*\bof=/dev/",
    r":\(\)\s*\{[^}]*\}\s*;", r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b",
    r"\bhalt\b", r">\s*/dev/sd", r"\bmv\s+/\s",
]

def safe_command(cmd: str) -> bool:
    return not any(re.search(p, cmd) for p in DENY)

def jail(path: str) -> str:
    p = os.path.realpath(os.path.join(WORKDIR, path.lstrip("/")))
    if not p.startswith(os.path.realpath(WORKDIR)):
        raise ValueError("path escapes workspace")
    return p

# ---------------- memory ----------------
def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS facts (k TEXT PRIMARY KEY, v TEXT, updated TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS log (t TEXT, role TEXT, text TEXT)")
    return c

def mem_save(k, v):
    c = db(); c.execute("REPLACE INTO facts VALUES (?,?,?)", (k, v, datetime.now().isoformat())); c.commit(); c.close()
    return {"saved": k}

def mem_get(k):
    c = db(); r = c.execute("SELECT v FROM facts WHERE k=?", (k,)).fetchone(); c.close()
    return {"value": r[0] if r else None}

def mem_all():
    c = db(); rows = c.execute("SELECT k,v FROM facts").fetchall(); c.close()
    return dict(rows)

# ---------------- tools ----------------
def t_run_shell(command, timeout=120):
    if not safe_command(command):
        return {"error": "BLOCKED: destructive pattern. Ask the owner first."}
    try:
        p = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=int(timeout), cwd=WORKDIR)
        return {"exit": p.returncode, "output": (p.stdout + p.stderr)[-6000:]}
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}

def t_read_file(path, limit=200):
    try:
        with open(jail(path)) as f:
            return {"content": "".join(f.readlines()[:int(limit)])}
    except Exception as e:
        return {"error": str(e)}

def t_write_file(path, content):
    try:
        p = jail(path); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return {"written": path, "bytes": len(content)}
    except Exception as e:
        return {"error": str(e)}

def t_web_fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Agent-1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "ignore")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return {"content": re.sub(r"\s+", " ", text)[:8000]}
    except Exception as e:
        return {"error": str(e)}

TOOL_IMPLS = {
    "run_shell": t_run_shell, "read_file": t_read_file, "write_file": t_write_file,
    "web_fetch": t_web_fetch, "memory_save": mem_save, "memory_get": mem_get,
}

TOOLS = [
    {"name": "run_shell", "description": "Run a shell command in the workspace. Destructive patterns are blocked.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a file in the workspace (jailed).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file in the workspace (jailed).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "web_fetch", "description": "Fetch a URL and return its text.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "memory_save", "description": "Save a durable fact (key/value) across sessions.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "memory_get", "description": "Recall a saved fact.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]

# ---------------- gemini ----------------
def gemini(contents):
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "tools": [{"function_declarations": TOOLS}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
    }
    req = urllib.request.Request(API + "?key=" + GEMINI_KEY,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)

def run_turn(user_text):
    facts = mem_all()
    ctx = "Known facts:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items()) if facts else "No saved facts yet."
    contents = [{"role": "user", "parts": [{"text": ctx + "\n\nUser: " + user_text}]}]
    for _ in range(MAX_STEPS):
        try:
            res = gemini(contents)
        except Exception as e:
            return f"Brain unreachable: {e}"
        cand = res.get("candidates", [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        texts = [p["text"] for p in parts if "text" in p]
        contents.append({"role": "model", "parts": parts})
        if not calls:
            final = " ".join(texts).strip()
            c = db(); c.execute("INSERT INTO log VALUES (?,?,?)",
                (datetime.now().isoformat(), "user", user_text[:500])); c.commit(); c.close()
            return final or "(no response)"
        fparts = []
        for call in calls:
            name, args = call["name"], call.get("args", {})
            try:
                out = TOOL_IMPLS[name](**args)
            except Exception as e:
                out = {"error": str(e)}
            fparts.append({"functionResponse": {"name": name, "response": out}})
        contents.append({"role": "user", "parts": fparts})
    return "Stopped after max steps."

# ---------------- telegram ----------------
def tg(method, payload=None):
    data = urllib.parse.urlencode(payload or {}).encode() if payload else None
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                                 data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def send(text):
    for i in range(0, max(len(text), 1), 4000):
        tg("sendMessage", {"chat_id": OWNER_ID, "text": text[i:i + 4000]})

def poll():
    offset = 0
    print("Agent-1 polling Telegram…", flush=True)
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 50})
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                if msg.get("from", {}).get("id") != OWNER_ID:
                    continue
                text = msg.get("text", "").strip()
                if not text:
                    continue
                print(f"< {text[:80]}", flush=True)
                reply = run_turn(text)
                print(f"> {reply[:80]}", flush=True)
                send(reply)
        except Exception as e:
            print("poll error:", e, flush=True)
            time.sleep(5)

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        send(run_turn(sys.argv[2]))
    else:
        poll()
