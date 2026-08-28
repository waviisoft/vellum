---
id: two-in-one
title: Two in one
since: spec-v1
---

# Two in one

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
