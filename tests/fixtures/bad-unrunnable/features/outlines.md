---
id: outlines
title: Outlines and templates that never run
since: spec-v1
---

# Outlines and templates that never run

The first three blocks below never execute; the last one runs. An unrunnable
scenario reads as coverage in the extracted suite while pinning nothing, which
is what `spec/decisions/2026-08-28-runnable-scenarios.md` has lint reject.

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

```gherkin
Feature: A template with no rows
  @id:template-with-empty-examples
  Scenario Template: Declares an Examples table with no data rows
    Given <n>
    Then it holds

    Examples:
      | n |
```

```gherkin
Feature: A template that runs
  @id:template-with-rows
  Scenario Template: Has a row, so it runs
    Given <n>
    Then it holds

    Examples:
      | n |
      | 1 |
```
