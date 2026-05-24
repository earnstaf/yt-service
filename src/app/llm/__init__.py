"""LLM provider abstraction layer (P2 A1 + extended in P4).

The single public entrypoint is :func:`execute`. Every task-specific module
under :mod:`app.tasks` composes its prompt and calls :func:`execute`, which
dispatches to the right provider/model per :data:`app.llm.routing.TASK_ROUTING`,
applies the daily cost cap, logs into ``llm_call_log``, and increments
Prometheus metrics. Provider clients live in sibling modules but never
exported directly — keep all callsites on the abstraction.
"""

from __future__ import annotations

from app.llm.fallback import LLMResponse, execute

__all__ = ["execute", "LLMResponse"]
