---
id: outlines
title: Outlines that never run
since: spec-v1
---

# Outlines that never run

Both blocks below parse cleanly and then never execute. They read as coverage
in the extracted suite while pinning nothing, which is what
`spec/decisions/2026-08-28-runnable-scenarios.md` has lint reject.

## Acceptance

```gherkin
Feature: No Examples at all
  @id:outline-without-examples
  Scenario Outline: Declares no Examples section
    Given <n>
    Then it holds
```

```gherkin
Feature: A header and no rows
  @id:outline-with-empty-examples
  Scenario Outline: Declares an Examples table with no data rows
    Given <n>
    Then it holds

    Examples:
      | n |
```
