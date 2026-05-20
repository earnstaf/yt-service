"""Stub for the Phase 3 channel/playlist ingest endpoint.

In Phase 1 there is no /v1/ingest endpoint. This script prints an
informational message to stderr and exits 0 so it can be invoked safely
without breaking pipelines.
"""

from __future__ import annotations

import sys


MESSAGE = (
    "Channel and playlist ingestion is added in Phase 3. Please pass individual "
    "video URLs to fetch_transcript.py for now."
)


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
