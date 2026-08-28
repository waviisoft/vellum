---
id: auth
title: Authentication
since: spec-v1
---

# Authentication

## Behavior

- Sessions expire after 30 idle minutes.

## Acceptance

```gherkin
Feature: Session expiry
  @id:auth-idle-session-expires
  Scenario: Idle session expires
    Given the reference environment
    And a signed-in user idle for 31 minutes
    When they request any authenticated page
    Then they are redirected to sign-in

  @id:auth-idle-threshold
  Scenario Outline: Idle threshold
    Given a signed-in user idle for <minutes> minutes
    When they request any authenticated page
    Then the result is "<outcome>"

    Examples:
      | minutes | outcome  |
      | 29      | allowed  |
      | 31      | redirect |
```

```gherkin
Feature: Sign-out
  @slow
  @id:auth-sign-out-clears-session
  Scenario: Sign-out clears the session
    Given a signed-in user
    When they sign out
    Then their session is gone
```
