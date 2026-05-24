"""Per-task orchestration modules.

Each module composes prompts, calls :func:`app.llm.execute`, parses results,
and persists via :mod:`app.cache`. Routes call these — never the LLM layer
directly.
"""
