"""Step definitions, one module per spec file that carries scenarios.

Importing this package registers every definition. `harness/run.py` imports it
once, before the runner starts, so a missing import shows up as an UNDEFINED
step rather than as a silently smaller suite.

**Seeded empty by `vellum init`, and empty is the honest state.** A freshly
provisioned installation has no step definitions, so every scenario in the
seeded spec tree reports UNDEFINED and `harness/run.py` exits 1. That is the
correct answer to "does this suite execute?", and it is the first thing to fix:
write one module per spec file that carries scenarios — `spec_pipeline.py` for
`spec/features/spec-pipeline.md`, and so on — and import them here:

    from steps import (  # noqa: F401
        billing,
    )

Two rules the registry enforces, worth knowing before the first module:

* A step's pattern is anchored at both ends, so matching is exact.
* Two definitions claiming one sentence is an `AmbiguousStep`, not a coin
  toss. Where two spec files share a sentence, one module owns it.
"""
