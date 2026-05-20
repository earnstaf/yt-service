"""Unit tests for ``app.metrics``.

Validates that the spec §10 collectors exist on the default Prometheus
registry, carry the expected label names, and that the exposition helpers
return well-formed bytes plus the canonical content type.
"""

from __future__ import annotations

import pytest
from prometheus_client import CONTENT_TYPE_LATEST

from app import metrics


EXPECTED_LABELS: dict[str, tuple[str, ...]] = {
    "yt_requests_total": ("endpoint", "status"),
    "yt_transcript_source_total": ("source",),
    "yt_job_duration_seconds": ("type",),
    "yt_cache_hits_total": ("table",),
    "yt_cache_misses_total": ("table",),
    "yt_llm_calls_total": ("task", "provider", "model", "status"),
    "yt_llm_cost_usd_total": ("provider", "task"),
    "yt_llm_latency_seconds": ("provider", "model"),
    "yt_whisper_cost_usd_total": (),
    "yt_active_jobs": ("type",),
    "yt_monitor_polls_total": ("monitor_id", "result"),
    "yt_webhook_deliveries_total": ("status",),
}


@pytest.mark.parametrize("metric_name, expected_labels", list(EXPECTED_LABELS.items()))
def test_metric_registered_with_expected_labels(
    metric_name: str, expected_labels: tuple[str, ...]
) -> None:
    # Counters and Histograms append suffixes (``_total``, ``_count``/``_sum``/``_bucket``)
    # so we look up by the base metric name via the registry mapping.
    families = {m.name: m for m in metrics.REGISTRY.collect()}

    # Counter exposition names end in ``_total``; the family name strips it.
    base_name = metric_name[:-6] if metric_name.endswith("_total") else metric_name
    assert base_name in families, f"metric {metric_name!r} not found in registry"

    # Sample label sets are empty until the metric is observed; instead use the
    # collector's stored _labelnames attribute on the underlying object.
    collector = _find_collector(metric_name)
    actual_labels = tuple(collector._labelnames)  # type: ignore[attr-defined]
    assert actual_labels == expected_labels


def _find_collector(name: str) -> object:
    """Look up the original collector object by exposition name."""
    name_map = {
        "yt_requests_total": metrics.REQUESTS_TOTAL,
        "yt_transcript_source_total": metrics.TRANSCRIPT_SOURCE_TOTAL,
        "yt_job_duration_seconds": metrics.JOB_DURATION,
        "yt_cache_hits_total": metrics.CACHE_HITS,
        "yt_cache_misses_total": metrics.CACHE_MISSES,
        "yt_llm_calls_total": metrics.LLM_CALLS,
        "yt_llm_cost_usd_total": metrics.LLM_COST_USD,
        "yt_llm_latency_seconds": metrics.LLM_LATENCY,
        "yt_whisper_cost_usd_total": metrics.WHISPER_COST_USD,
        "yt_active_jobs": metrics.ACTIVE_JOBS,
        "yt_monitor_polls_total": metrics.MONITOR_POLLS,
        "yt_webhook_deliveries_total": metrics.WEBHOOK_DELIVERIES,
    }
    return name_map[name]


def test_render_metrics_returns_bytes() -> None:
    payload = metrics.render_metrics()
    assert isinstance(payload, bytes)
    assert len(payload) > 0
    # The exposition format always carries ``# HELP`` lines for every metric.
    assert b"yt_requests_total" in payload
    assert b"yt_llm_calls_total" in payload


def test_content_type_matches_prometheus_default() -> None:
    assert metrics.content_type() == CONTENT_TYPE_LATEST


def test_counter_observation_increments_exposition() -> None:
    before = metrics.render_metrics()
    metrics.CACHE_HITS.labels(table="transcripts").inc()
    after = metrics.render_metrics()
    # Exposition should now mention the labeled series.
    assert b'yt_cache_hits_total{table="transcripts"}' in after
    assert after != before
