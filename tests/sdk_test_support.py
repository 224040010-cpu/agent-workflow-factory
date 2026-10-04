"""Local streaming model fixture: no credentials or external API calls."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


@contextmanager
def local_model_endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            answer = {"status": "completed", "facts": {}, "evidence": ["local-test"]}
            for message in reversed(body.get("messages", [])):
                if message.get("role") != "user":
                    continue
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(block.get("text", "") for block in content)
                try:
                    payload = json.loads(content)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and "required_response" in payload:
                    answer = payload["required_response"]
                    break
            chunk = {"id": "local-completion", "object": "chat.completion.chunk", "created": 1,
                     "model": "deepseek-v4-flash", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": json.dumps(answer)}, "finish_reason": None}]}
            final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}}
            data = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
