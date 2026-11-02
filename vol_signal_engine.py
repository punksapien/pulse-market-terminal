"""ADR-0029: alias the module so existing mocks patch canonical function globals."""
import sys

from pulse.legacy import signal_engine as _implementation

sys.modules[__name__] = _implementation
