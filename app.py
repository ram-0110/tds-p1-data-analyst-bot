#!/usr/bin/env python3
"""Persistent Telegram data-analysis agent using Ollama and public JSONL logs."""

from __future__ import annotations

import ast
import html
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_local_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env(ROOT / ".env")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "8"))
USER_AGENT = "TDS-P1-Data-Analyst/1.0"
SEARCH_USER_AGENT = "Mozilla/5.0"
CHAT_HISTORY: dict[int, list[str]] = {}
HISTORY_LOCK = threading.Lock()


def request_json(url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def append_log(path: Path, event: str, **fields: object) -> None:
    row = {"ts": time.time(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def clean_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def web_search(query: str) -> str:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": SEARCH_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as response:
        page = response.read().decode("utf-8", errors="replace")
    results = []
    pattern = re.compile(
        r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>'
        r'(.*?)</a>.*?<p[^>]*>(.*?)</p>',
        re.I | re.S,
    )
    for href, title, snippet in pattern.findall(page)[:8]:
        href = html.unescape(href)
        results.append(
            json.dumps(
                {
                    "title": clean_text(title),
                    "url": href,
                    "snippet": clean_text(snippet),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(results) if results else clean_text(page)[:6000]


def fetch_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only public HTTP(S) URLs are supported")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read(2_000_000)
        content_type = response.headers.get("Content-Type", "")
    if "pdf" in content_type.lower() or body.startswith(b"%PDF"):
        return "PDF detected. Search for an HTML/CSV mirror or a table-specific source."
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        text = clean_text(text)
    return text[:30_000]


ALLOWED_IMPORTS = {
    "csv",
    "json",
    "math",
    "statistics",
    "re",
    "datetime",
    "collections",
    "itertools",
    "decimal",
    "fractions",
}
FORBIDDEN_NAMES = {
    "open",
    "eval",
    "exec",
    "compile",
    "input",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
}


def calculate(code: str) -> str:
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [item.name.split(".")[0] for item in node.names]
            if any(name not in ALLOWED_IMPORTS for name in names):
                raise ValueError(f"Import not allowed: {names}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"Name not allowed: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("Dunder access is not allowed")
    safe_builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "print": print,
        "range": range,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "__import__": __import__,
    }
    output: list[str] = []
    safe_builtins["print"] = lambda *args, **_: output.append(" ".join(map(str, args)))
    scope = {"__builtins__": safe_builtins}
    exec(compile(tree, "<calculation>", "exec"), scope, scope)
    return "\n".join(output)[-12_000:] or repr(scope.get("result", "No result printed"))


SYSTEM_PROMPT = """You are a careful autonomous data analyst. Solve the LAST user message,
using earlier messages only as conversation context. The requested final answer shape is
spelled out in the message. Research authoritative public sources and calculate exactly.

Return exactly ONE compact JSON object on each turn. Valid actions:
{"tool":"search","query":"specific web search"}
{"tool":"fetch","url":"https://public-url"}
{"tool":"python","code":"print(...) using only csv/json/math/statistics/re/datetime/collections/itertools"}
{"tool":"final","answer":<the answer object/value in the exact requested shape>}

Do not wrap JSON in markdown. Use search/fetch/calculation before finalizing unless the
answer is explicitly present in the prompt. Cross-check factual answers. Never include
the outer answer/log_url wrapper; the application adds it. Never invent a source."""


def parse_action(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Model action is not an object")
    return value


def ollama(messages: list[dict]) -> str:
    response = request_json(
        f"{OLLAMA_URL}/api/chat",
        {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": 16384},
        },
        timeout=120,
    )
    return response["message"]["content"]


def solve(history: list[str], log_path: Path) -> object:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append(
        {
            "role": "user",
            "content": "Conversation:\n"
            + "\n".join(f"{i + 1}. {text}" for i, text in enumerate(history[-6:])),
        }
    )
    append_log(log_path, "input", history=history[-6:], model=OLLAMA_MODEL)
    for step in range(1, MAX_STEPS + 1):
        raw = ollama(messages)
        append_log(log_path, "model", step=step, content=raw)
        try:
            action = parse_action(raw)
            tool = action.get("tool")
            if tool == "final":
                if "answer" not in action:
                    raise ValueError("Final action omitted answer")
                append_log(log_path, "final", answer=action["answer"])
                return action["answer"]
            if tool == "search":
                result = web_search(str(action["query"]))
            elif tool == "fetch":
                result = fetch_url(str(action["url"]))
            elif tool == "python":
                result = calculate(str(action["code"]))
            else:
                raise ValueError(f"Unknown tool: {tool!r}")
            append_log(log_path, "tool", step=step, tool=tool, result=result[:8000])
            messages.extend(
                [
                    {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)},
                    {"role": "user", "content": f"Tool result:\n{result[:12000]}"},
                ]
            )
        except Exception as exc:
            append_log(log_path, "step_error", step=step, error=str(exc))
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"Invalid action or failed tool: {exc}. Return one valid action JSON.",
                    },
                ]
            )
    raise RuntimeError("Agent exhausted its step budget")


class LogHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(
                {
                    "ok": True,
                    "model": OLLAMA_MODEL,
                    "telegram_configured": bool(BOT_TOKEN),
                    "public_base_url_configured": bool(PUBLIC_BASE_URL),
                }
            )
            return
        match = re.fullmatch(r"/logs/([A-Za-z0-9_-]+\.jsonl)", self.path)
        if not match:
            self.send_error(404)
            return
        path = LOG_DIR / match.group(1)
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value: object) -> None:
        data = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: object) -> None:
        return


def send_message(chat_id: int, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    request_json(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        {"chat_id": chat_id, "text": text},
        timeout=30,
    )


def process_message(message: dict, update_id: int) -> None:
    chat_id = int(message["chat"]["id"])
    text = str(message.get("text", "")).strip()
    if not text:
        return
    with HISTORY_LOCK:
        history = CHAT_HISTORY.setdefault(chat_id, [])
        history.append(text)
        del history[:-6]
        current_history = list(history)
    log_name = f"run-{chat_id}-{update_id}-{uuid.uuid4().hex[:8]}.jsonl"
    log_path = LOG_DIR / log_name
    try:
        answer = solve(current_history, log_path)
    except Exception as exc:
        append_log(log_path, "fatal", error=str(exc), traceback=traceback.format_exc())
        answer = {"error": "analysis_failed"}
    base = PUBLIC_BASE_URL or f"http://{HOST}:{PORT}"
    send_message(
        chat_id,
        {"answer": answer, "log_url": f"{base}/logs/{log_name}"},
    )


def telegram_loop() -> None:
    if not BOT_TOKEN:
        print("BOT_TOKEN is not configured; log server only.")
        return
    me = request_json(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=30)
    print(f"Telegram bot ready: @{me['result']['username']}")
    offset = 0
    while True:
        try:
            response = request_json(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                {"offset": offset, "timeout": 50, "allowed_updates": ["message"]},
                timeout=60,
            )
            for update in response.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                if "message" in update:
                    threading.Thread(
                        target=process_message,
                        args=(update["message"], int(update["update_id"])),
                        daemon=True,
                    ).start()
        except Exception as exc:
            print(f"Telegram polling error: {exc}")
            time.sleep(5)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), LogHandler)
    threading.Thread(target=telegram_loop, daemon=True).start()
    print(f"Log server listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
