"""Isolated local acceptance target: no credentials or business data."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload=json.dumps({'status':'ok', 'service':'kiwi-ci-demo'}).encode()
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers();self.wfile.write(payload)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
