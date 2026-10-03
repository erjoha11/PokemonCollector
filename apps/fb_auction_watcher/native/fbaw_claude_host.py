#!/usr/bin/python3
"""Native messaging host: lets the FB Auction Watcher extension ask Claude Code.

Chrome starts this script for each request from the extension (and only that extension; see
allowed_origins in the host manifest written by install.sh). It reads one JSON message
({system, input, schema, task}), runs `claude -p` on the user's own Claude login with the
smallest model and no tools, and writes back the structured answer. No API key, nothing
stored, nothing written anywhere.

Protocol (Chrome native messaging): each message is a 4-byte little-endian length, then that
many bytes of UTF-8 JSON, on stdin/stdout.
"""

import json
import os
import pwd
import shutil
import struct
import subprocess
import sys
import tempfile

TIMEOUT_S = 180
MODEL = "haiku"
# Chrome starts hosts with a minimal PATH; look where Claude Code is usually installed too.
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.claude/local")]
NO_TOOLS = "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,Agent,Task,NotebookEdit"


def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def find_claude():
    path = os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_PATHS)
    return shutil.which("claude", path=path)


def ask_claude(msg):
    claude = find_claude()
    if not claude:
        return {"ok": False, "error": "Claude Code (claude) not found on this Mac"}
    cmd = [
        claude, "-p",
        "--model", MODEL,
        "--system-prompt", msg["system"],
        "--json-schema", json.dumps(msg["schema"]),
        "--output-format", "json",
        "--disallowedTools", NO_TOOLS,
        "--max-turns", "3",
    ]
    # Claude Code reads its login from the macOS keychain, which needs the user's name; Chrome
    # usually passes USER/LOGNAME on, but fill them in if not.
    env = dict(os.environ)
    user = pwd.getpwuid(os.getuid()).pw_name
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    env.setdefault("HOME", os.path.expanduser("~"))
    # Run outside any project so no CLAUDE.md or project settings get picked up.
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(cmd, input=msg["input"], capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"claude -p took more than {TIMEOUT_S} s"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-500:] or f"claude exited with {proc.returncode}"}
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "claude -p returned something that isn't JSON"}
    if out.get("is_error") or out.get("structured_output") is None:
        return {"ok": False, "error": str(out.get("result") or out.get("subtype") or "no structured output")[:500]}
    return {
        "ok": True,
        "result": out["structured_output"],
        "durationMs": out.get("duration_ms"),
        "usage": {k: out.get("usage", {}).get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")},
    }


def main():
    msg = read_message()
    if msg is None:
        return
    if msg.get("ping"):
        send_message({"ok": True, "pong": True, "claude": find_claude()})
        return
    try:
        send_message(ask_claude(msg))
    except Exception as err:  # Report anything unexpected to the extension instead of dying silently.
        send_message({"ok": False, "error": f"host error: {err}"})


if __name__ == "__main__":
    main()
