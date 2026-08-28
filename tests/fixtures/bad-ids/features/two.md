---
id: two
title: Two
since: spec-v1
---

# Two

## Acceptance

```gherkin
Feature: Identity elsewhere
  @id:shared-id
  Scenario: Claims the same id from a different file
    Given a thing
    Then it holds
```
