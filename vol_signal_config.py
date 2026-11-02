"""ADR-0029: old import path, same canonical configuration classes."""
import sys

from pulse.legacy import signal_config as _implementation

sys.modules[__name__] = _implementation
