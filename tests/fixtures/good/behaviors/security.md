---
id: security
title: Security
since: spec-v1
---

# Security

## Acceptance

```gherkin
Feature: Dependency policy
  Scenario: An unlisted registry fails verification
    Given a PR adding a dependency from an unlisted registry
    When the verifier reviews the PR
    Then the review fails
```
