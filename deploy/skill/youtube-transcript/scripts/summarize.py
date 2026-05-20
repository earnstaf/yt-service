"""Stub for the Phase 2 summarize endpoint.

In Phase 1 there is no /v1/summarize endpoint. This script prints an
informational message to stderr and exits 0 so it can be invoked safely
without breaking pipelines.
"""

from __future__ import annotations

import sys


MESSAGE = (
    "The summarize endpoint is added in Phase 2. Please use fetch_transcript.py "
    "for now and ask Claude to summarize the returned text."
)


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
