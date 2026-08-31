---
id: mixed
title: One good block and one broken one
since: spec-v1
---

# One good block and one broken one

A tree whose blocks do not all parse, but which is not empty: the good block
below extracts cleanly, so a suite emitted from this tree would look ordinary —
one scenario, no error — and the broken block's scenario would simply not be in
it. That silent shortfall is what `extract` refuses (waviisoft/vellum#7); the
`bad-gherkin` fixture next door cannot show it, because nothing in that tree
parses and an empty suite is at least conspicuous.

## Acceptance

```gherkin
Feature: Readable
  @id:mixed-good-block
  Scenario: This one parses
    Given a well-formed block
    When the suite is extracted
    Then this scenario is in it
```

The docstring below is never closed, so the block runs off the end:

```gherkin
Feature: Unreadable
  @id:mixed-broken-block
  Scenario: This one does not parse
    Given a payload
      """
      {"still": "open"
    Then it is rejected
```
