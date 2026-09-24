"""Accuracy-evaluation harness: gold-set schema, deterministic scoring,
statistics, a runner that drives `QueryService`, and a report builder.

The real-model eval is never part of the normal test suite; the unit tests in
`tests/unit/eval/` exercise everything here against `FakeInferenceGateway`.
"""
