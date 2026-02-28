#!/usr/bin/env python3
"""HTTP-to-stdio MCP bridge for HomeClaw.

Spawns a long-running HomeClaw MCP subprocess and exposes it over HTTP
so Docker containers can reach it via the network.

Usage:
    ./homeclaw-http-bridge.py [--port 9876] [--command "homeclaw-cli mcp"]
"""

import argparse
import json
import signal
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

proc: subprocess.Popen | None = None
proc_lock = threading.Lock()


class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            request = json.loads(body)
        except Exception:
            self._reply(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})
            return

        with proc_lock:
            if proc is None or proc.poll() is not None:
                self._reply(502, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Subprocess not running"}, "id": request.get("id")})
                return
            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("Empty response from subprocess")
                response = json.loads(line)
            except Exception as exc:
                self._reply(502, {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(exc)}, "id": request.get("id")})
                return

        self._reply(200, response)

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
    parser = argparse.ArgumentParser(description="HTTP-to-stdio MCP bridge")
    parser.add_argument("--port", type=int, default=9876, help="Port to listen on (default: 9876)")
    parser.add_argument("--command", default="homeclaw-cli mcp", help="Subprocess command (default: homeclaw-cli mcp)")
    args = parser.parse_args()

    global proc
    proc = subprocess.Popen(
        args.command.split(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )
    print(f"[bridge] Subprocess started: {args.command}", file=sys.stderr)

    server = HTTPServer(("127.0.0.1", args.port), BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[bridge] Listening on http://127.0.0.1:{args.port}", file=sys.stderr)

    def shutdown(signum, frame):
        print(f"[bridge] Shutting down (signal {signum})", file=sys.stderr)
        server.shutdown()
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.pause()


if __name__ == "__main__":
    main()
