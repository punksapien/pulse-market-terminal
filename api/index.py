"""ADR-0030: Vercel adapter; one canonical service for imports and existing mocks."""
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pulse.legacy import service as _implementation

# ADR-0091: Vercel discovers explicit classes, not the canonical alias below.
class handler(_implementation.handler, BaseHTTPRequestHandler):
    pass


# Some loaders retain the executed module object instead of looking in sys.modules.
handler = _implementation.handler
sys.modules[__name__] = _implementation
