"""Entrypoint for ``python -m sero.web``: migrate, then serve."""

from __future__ import annotations

import sys

from sero.web.boot import main

if __name__ == "__main__":
    sys.exit(main())
