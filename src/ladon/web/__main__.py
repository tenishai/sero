"""Entrypoint for ``python -m ladon.web``: migrate, then serve."""

from __future__ import annotations

import sys

from ladon.web.boot import main

if __name__ == "__main__":
    sys.exit(main())
