---
id: two-in-one
title: Two in one
since: spec-v1
---

# Two in one

Two Features in one fence: legal in this tree until spec-v4, and unreadable by
a stock Cucumber parser, which stops at the second `Feature:`.

## Acceptance

```gherkin
Feature: First concern
  @id:two-in-one-first
  Scenario: The first concern works
    Given a thing
    Then it works

Feature: Second concern
  @id:two-in-one-second
  Scenario: The second concern works
    Given another thing
    Then it also works
```
