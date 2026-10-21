"""The 4-stage regulation-internalization pipeline.

Stages (each ``async def run(ctx) -> dict`` returning regulation field updates):

1. ``applicability`` — document-level, does the regulation apply to us
2. ``split_stage``   — split into articles (MinerU first, LLM fallback)
3. ``internalize``   — per-article internalization judgment
4. ``analysis``      — obligation decomposition + ReAct compare + report

The 1→4 chain with skip logic lives in ``clause_splitter.orchestrator.run_chain``
(the engine-wiring composition root); ``contexts`` wires per-job state.
Stages raise :class:`StageError` on failure (job → failed); stage code never
touches job rows — the runner owns job state transitions.
"""
