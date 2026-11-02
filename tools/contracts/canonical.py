"""Compatibility facade; deployed worker package owns canonical identity."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.wire.identity import digest,generation,seal_fixture
