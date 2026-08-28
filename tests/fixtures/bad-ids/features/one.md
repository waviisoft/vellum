---
id: one
title: One
since: spec-v1
---

# One

## Acceptance

```gherkin
Feature: Identity
  @id:shared-id
  Scenario: Claims an id another file also claims
    Given a thing
    Then it holds

  Scenario: Declares no id at all
    Given a thing
    Then it holds

  @id:Not_A_Slug
  Scenario: Declares a malformed id
    Given a thing
    Then it holds

  @id:first-id
  @id:second-id
  Scenario: Declares two ids
    Given a thing
    Then it holds
```

An outline with no rows parses, and then never runs:

```gherkin
Feature: Empty outline
  @id:empty-outline
  Scenario Outline: Has no examples
    Given <n>
```
