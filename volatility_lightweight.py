"""ADR-0029: compatibility import and CLI; implementation lives in pulse.legacy."""
import sys

from pulse.legacy import scoring as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
