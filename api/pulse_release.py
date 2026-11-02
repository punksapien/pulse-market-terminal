"""Secret-protected Preview-only release setup; never called by a viewer."""
from http.server import BaseHTTPRequestHandler
import os
from urllib.parse import urlsplit

from pulse.http.worker_trigger import response
from pulse.http.published_document import error_response


def run(**kwargs):
    from pulse.collectors.release_setup import run as setup
    return setup(**kwargs)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        result=(error_response(404,"preview_only") if os.environ.get("VERCEL_ENV")!="preview" else
            response(method="POST",authorization=self.headers.get("Authorization"),
            content_length=self.headers.get("Content-Length"),query=urlsplit(self.path).query,
            environment=os.environ.get("VERCEL_ENV"),config=os.environ,runner=run))
        self.send_response(result.status)
        for name,value in result.headers.items():self.send_header(name,value)
        self.end_headers();self.wfile.write(result.body)
