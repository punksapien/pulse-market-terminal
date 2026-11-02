"""ADR-0029: preserve one provider cooldown dictionary and lock across import paths."""
import sys

from pulse.legacy import signal_oi as _implementation

sys.modules[__name__] = _implementation
