#!/usr/bin/env python3
"""tiny-llm---minimal OpenAI-compatible stub for testing scandbox network paths.

No real model, no GPU, no dependencies beyond the standard library. Responds to:
  GET  /health                → {"status":"ok"}
  GET  /v1/models             → OpenAI models list
  POST /v1/chat/completions   → canned completion (echoes back the last message)
"""
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

MODEL_ID = "tiny-llm-stub"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model", "owned_by": "scandbox-test"}],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/v1/chat/completions":
            messages = body.get("messages", [])
            last = messages[-1]["content"] if messages else ""
            self._json(200, {
                "id": f"chatcmpl-stub-{int(time.time())}",
                "object": "chat.completion",
                "model": body.get("model", MODEL_ID),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"I am {MODEL_ID}. You said: {last[:200]}",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": max(len(last.split()), 1),
                    "completion_tokens": 15,
                    "total_tokens": max(len(last.split()), 1) + 15,
                },
            })
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[tiny-llm] {fmt % args}", flush=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    a = p.parse_args()
    srv = HTTPServer((a.host, a.port), Handler)
    print(f"tiny-llm: listening on {a.host}:{a.port}", flush=True)
    srv.serve_forever()
