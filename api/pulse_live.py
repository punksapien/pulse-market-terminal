"""Secret-protected Preview-only fast live publisher, never a viewer route."""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
import os
from pulse.http.worker_trigger import response
from pulse.http.published_document import error_response


def run(**kwargs):
    from pulse.collectors.live_runtime import run as publish
    return publish(**kwargs)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):self._respond("GET")
    def do_POST(self):self._respond("POST")
    def _respond(self,method):
        result=response(
            method=method,authorization=self.headers.get("Authorization"),content_length=self.headers.get("Content-Length"),
            query=urlsplit(self.path).query,environment=os.environ.get("VERCEL_ENV"),config=os.environ,runner=run)
        self.send_response(result.status)
        for k,v in result.headers.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(result.body)
