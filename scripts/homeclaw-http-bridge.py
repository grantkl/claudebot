#!/usr/bin/env python3.11
"""HTTP-to-stdio MCP bridge for HomeClaw.

Spawns a fresh MCP subprocess per request to avoid stale protocol state.
Each request gets its own initialize → method → response cycle.

Usage:
    ./homeclaw-http-bridge.py [--port 9876] [--command "node /path/to/mcp-server.js"]
"""

import argparse
import json
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

COMMAND: list[str] = []

INIT_REQUEST = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "homeclaw-bridge", "version": "1.0"},
    },
    "id": "__init__",
}


def _call_mcp(request: dict) -> dict:
    """Spawn subprocess, initialize MCP, send request, return response."""
    proc = subprocess.Popen(
        COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        # Initialize the MCP session
        proc.stdin.write(json.dumps(INIT_REQUEST) + "\n")
        proc.stdin.flush()
        init_line = proc.stdout.readline()
        if not init_line:
            raise RuntimeError("No response to initialize")

        # Send the actual request
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("No response from subprocess")
        return json.loads(line)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            request = json.loads(body)
        except Exception:
            self._reply(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})
            return

        method = request.get("method", "")

        # Intercept initialize — we handle it internally per-request
        if method == "initialize":
            proc = subprocess.Popen(
                COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                self._reply(200, json.loads(line) if line else {})
            finally:
                proc.terminate()
                proc.wait(timeout=5)
            return

        # Skip notifications (no id = no response expected)
        if "id" not in request:
            self._reply(200, {})
            return

        try:
            response = _call_mcp(request)
            self._reply(200, response)
        except Exception as exc:
            self._reply(502, {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(exc)}, "id": request.get("id")})

    def _reply(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"[bridge] {fmt % args}", file=sys.stderr)


def main():
    global COMMAND
    parser = argparse.ArgumentParser(description="HTTP-to-stdio MCP bridge")
    parser.add_argument("--port", type=int, default=9876, help="Port to listen on (default: 9876)")
    parser.add_argument("--command", default="/opt/homebrew/bin/node /Applications/HomeClaw.app/Contents/Resources/mcp-server.js",
                        help="Subprocess command (default: node mcp-server.js)")
    args = parser.parse_args()
    COMMAND = args.command.split()

    server = HTTPServer(("127.0.0.1", args.port), BridgeHandler)
    print(f"[bridge] Listening on http://127.0.0.1:{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
