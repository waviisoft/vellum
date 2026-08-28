---
id: shared-setup
title: Shared setup
since: spec-v1
---

# Shared setup

## Acceptance

```gherkin
Feature: Sign-in
  Background:
    Given the reference environment
    And a registered user

  @id:shared-setup-good-password
  Scenario: Good password
    When they sign in
    Then they see the dashboard

  @id:shared-setup-bad-password
  Scenario: Bad password
    When they mistype
    Then they see an error
```
