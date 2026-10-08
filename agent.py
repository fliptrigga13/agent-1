#!/usr/bin/env python3
"""
agent-1: persistent personal agent for Lauren Flipo.
Brain: Gemini 3.8 Flash via API + function calling.
UI: private HTTPS web chat (password) + optional Telegram polling.
Memory: SQLite (facts) + conversation log.
Safety: destructive-command denylist, non-root user, owner-only access.
Zero third-party dependencies — stdlib only.

Modes:
  python3 agent.py            # serve web UI (+ Telegram if configured)
  python3 agent.py --once "…" # single turn (for cron); uses Telegram if configured
"""
import html
import http.server
import json
import os
import re
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime

# ---------------- config ----------------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or sys.exit("missing GEMINI_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID") or 0)
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
API = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
WORKDIR = os.environ.get("AGENT_WORKDIR", os.path.expanduser("~/workspace"))
DB = os.path.join(WORKDIR, "memory.db")
WEB_PORT = int(os.environ.get("WEB_PORT", "8443"))
TLS_CERT = os.environ.get("TLS_CERT", "")
TLS_KEY = os.environ.get("TLS_KEY", "")
MAX_STEPS = 10

TELEGRAM_ON = bool(TG_TOKEN and not TG_TOKEN.startswith("PASTE") and OWNER_ID)

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
            page = r.read().decode("utf-8", "ignore")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
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

# ---------------- telegram (optional) ----------------
def tg(method, payload=None):
    data = urllib.parse.urlencode(payload or {}).encode() if payload else None
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                                 data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def tg_send(text):
    for i in range(0, max(len(text), 1), 4000):
        tg("sendMessage", {"chat_id": OWNER_ID, "text": text[i:i + 4000]})

def tg_poll():
    offset = 0
    print("Telegram polling on.", flush=True)
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 50})
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                if msg.get("from", {}).get("id") != OWNER_ID:
                    continue
                text = msg.get("text", "").strip()
                if text:
                    tg_send(run_turn(text))
        except Exception as e:
            print("poll error:", e, flush=True)
            time.sleep(5)

# ---------------- web UI ----------------
CHAT_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent-1</title>
<style>
*{box-sizing:border-box;margin:0}body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;height:100dvh;display:flex;flex-direction:column}
header{padding:12px 16px;border-bottom:1px solid #21262d;font-weight:700}#log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.m{max-width:85%;padding:10px 14px;border-radius:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}
.u{align-self:flex-end;background:#1f6feb}.a{align-self:flex-start;background:#161b22;border:1px solid #21262d}
#bar{display:flex;gap:8px;padding:10px;border-top:1px solid #21262d}
#in{flex:1;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:10px;padding:10px;font-size:16px}
#send{background:#1f6feb;color:#fff;border:0;border-radius:10px;padding:10px 18px;font-size:16px}
#lock{padding:40px 20px;text-align:center}#lock input{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:10px;padding:12px;font-size:16px;width:100%;max-width:300px;margin-bottom:10px}
.typ{opacity:.6;font-style:italic}
</style></head><body>
<header>Agent-1</header>
<div id="lock"><h3>Enter password</h3><br><input id="pw" type="password" placeholder="password"><br><button id="send" onclick="unlock()">Unlock</button></div>
<div id="log" style="display:none"></div>
<div id="bar" style="display:none"><input id="in" placeholder="Message Agent-1…" autocomplete="off"><button id="send" onclick="send()">Send</button></div>
<script>
let token=localStorage.getItem('a1')||'';
function showLock(o){document.getElementById('lock').style.display=o?'block':'none';document.getElementById('log').style.display=o?'none':'flex';document.getElementById('bar').style.display=o?'none':'flex';}
function unlock(){token=document.getElementById('pw').value;localStorage.setItem('a1',token);showLock(false);add('a','Unlocked. Say hi.');}
function add(c,t){const d=document.createElement('div');d.className='m '+c;d.textContent=t;document.getElementById('log').appendChild(d);document.getElementById('log').scrollTop=1e9;return d;}
async function send(){const i=document.getElementById('in');const t=i.value.trim();if(!t)return;i.value='';add('u',t);const w=add('a','…');w.classList.add('typ');
try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Token':token},body:JSON.stringify({text:t})});
if(r.status===401){showLock(true);w.remove();return;}const j=await r.json();w.classList.remove('typ');w.textContent=j.reply||'(empty)';}catch(e){w.classList.remove('typ');w.textContent='Connection error.';}}
document.getElementById('in').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')unlock();});
if(token)showLock(false);
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            self._send(200, CHAT_HTML, "text/html")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path != "/api/chat":
            return self._send(404, "not found")
        if not WEB_PASSWORD or self.headers.get("X-Auth-Token") != WEB_PASSWORD:
            return self._send(401, "unauthorized")
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            reply = run_turn(data.get("text", ""))
            self._send(200, json.dumps({"reply": reply}), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"reply": f"error: {e}"}), "application/json")

def serve_web():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("0.0.0.0", WEB_PORT), Handler)
    server.daemon_threads = True
    if TLS_CERT and TLS_KEY and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"Web UI: https://0.0.0.0:{WEB_PORT} (TLS)", flush=True)
    else:
        print(f"Web UI: http://0.0.0.0:{WEB_PORT} (no TLS cert found)", flush=True)
    server.serve_forever()

# ---------------- main ----------------
if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        if TELEGRAM_ON:
            tg_send(run_turn(sys.argv[2]))
        else:
            print(run_turn(sys.argv[2]))
    else:
        if WEB_PASSWORD:
            threading.Thread(target=serve_web, daemon=True).start()
        else:
            print("WEB_PASSWORD not set — web UI disabled.", flush=True)
        if TELEGRAM_ON:
            tg_poll()
        else:
            print("Telegram not configured — web UI only.", flush=True)
            while True:
                time.sleep(3600)
