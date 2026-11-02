"""Authenticated fixed-schedule publisher. Never called by the public reader."""
from http.server import BaseHTTPRequestHandler
import os
from urllib.parse import urlsplit
from pulse.http.worker_trigger import response


def run(**kwargs):
    from pulse.collectors.snapshot_runtime import run as publish
    return publish(**kwargs)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):self._respond("GET")
    def do_POST(self):self._respond("POST")
    def _respond(self,method):
        result=response(method=method,authorization=self.headers.get("Authorization"),
            content_length=self.headers.get("Content-Length"),query=urlsplit(self.path).query,
            environment=os.environ.get("VERCEL_ENV"),config=os.environ,runner=run)
        self.send_response(result.status)
        for name,value in result.headers.items():self.send_header(name,value)
        self.end_headers();self.wfile.write(result.body)
