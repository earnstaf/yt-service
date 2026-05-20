"""Prometheus metric collectors for yt-transcript-service (spec §10).

All collectors register on the default ``prometheus_client.REGISTRY`` so the
``/metrics`` endpoint and any middleware can collect without wiring custom
registries. The collectors are module-level so they survive process lifetime
and aggregate across all requests.

Metric names follow the ``yt_`` prefix convention from the spec; labels are
kept low-cardinality so Prometheus storage stays cheap.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REQUESTS_TOTAL = Counter(
    "yt_requests_total",
    "HTTP requests",
    ["endpoint", "status"],
)

TRANSCRIPT_SOURCE_TOTAL = Counter(
    "yt_transcript_source_total",
    "Transcript outcomes by source",
    ["source"],
)

JOB_DURATION = Histogram(
    "yt_job_duration_seconds",
    "Job duration",
    ["type"],
    buckets=(1, 5, 15, 30, 60, 180, 600, 1800),
)

CACHE_HITS = Counter(
    "yt_cache_hits_total",
    "Cache hits",
    ["table"],
)

CACHE_MISSES = Counter(
    "yt_cache_misses_total",
    "Cache misses",
    ["table"],
)

LLM_CALLS = Counter(
    "yt_llm_calls_total",
    "LLM calls",
    ["task", "provider", "model", "status"],
)

LLM_COST_USD = Counter(
    "yt_llm_cost_usd_total",
    "LLM cost in USD",
    ["provider", "task"],
)

LLM_LATENCY = Histogram(
    "yt_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

WHISPER_COST_USD = Counter(
    "yt_whisper_cost_usd_total",
    "Whisper cost in USD",
    [],
)

ACTIVE_JOBS = Gauge(
    "yt_active_jobs",
    "Currently running jobs",
    ["type"],
)

MONITOR_POLLS = Counter(
    "yt_monitor_polls_total",
    "Monitor poll results",
    ["monitor_id", "result"],
)

WEBHOOK_DELIVERIES = Counter(
    "yt_webhook_deliveries_total",
    "Webhook delivery attempts",
    ["status"],
)


def render_metrics() -> bytes:
    """Return the current registry exposition as bytes for the ``/metrics`` route."""
    return generate_latest(REGISTRY)


def content_type() -> str:
    """Return the Prometheus exposition Content-Type header value."""
    return CONTENT_TYPE_LATEST


__all__ = [
    "ACTIVE_JOBS",
    "CACHE_HITS",
    "CACHE_MISSES",
    "JOB_DURATION",
    "LLM_CALLS",
    "LLM_COST_USD",
    "LLM_LATENCY",
    "MONITOR_POLLS",
    "REGISTRY",
    "REQUESTS_TOTAL",
    "TRANSCRIPT_SOURCE_TOTAL",
    "WEBHOOK_DELIVERIES",
    "WHISPER_COST_USD",
    "content_type",
    "render_metrics",
]
