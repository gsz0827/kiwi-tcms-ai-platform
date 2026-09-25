"""Deterministic, stateless demo API. Only exposed on the Compose network."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload, cookie=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.reply(200, {"status": "ok"})
        elif path == "/users/1":
            self.reply(200, {"data": {"id": 1, "name": "demo"}})
        elif path == "/cookie/me":
            if "demo_session=demo-cookie-not-a-real-credential" in self.headers.get("Cookie", "").split("; "):
                self.reply(200, {"data": {"id": 1, "name": "demo"}})
            else:
                self.reply(401, {"error": "authentication_required"})
        elif path == "/me":
            if self.headers.get("Authorization") == "Bearer demo-token-not-a-real-credential":
                self.reply(200, {"data": {"id": 1, "name": "demo"}})
            else:
                self.reply(401, {"error": "authentication_required"})
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                self.reply(413, {"error": "body_too_large"})
                return
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "invalid_json"})
            return
        if urlsplit(self.path).path not in ("/login", "/cookie/login"):
            self.reply(404, {"error": "not_found"})
        elif body == {"username": "demo", "password": "demo-password"}:
            self.reply(200, {"token": "demo-token-not-a-real-credential", "user_id": 1},
                       cookie="demo_session=demo-cookie-not-a-real-credential; Path=/cookie/; HttpOnly; SameSite=Lax"
                       if urlsplit(self.path).path == "/cookie/login" else None)
        else:
            self.reply(401, {"error": "invalid_credentials"})

    def log_message(self, format, *args):
        pass  # Never log credentials or query strings.


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
