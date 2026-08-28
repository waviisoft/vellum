---
id: broken
title: Broken scenarios
since: spec-v1
---

# Broken scenarios

## Acceptance

An unterminated docstring runs off the end of the block:

```gherkin
Feature: Unterminated docstring
  Scenario: The quote is never closed
    Given a payload
      """
      {"still": "open"
    Then it is rejected
```

A line that is neither a keyword nor a step:

```gherkin
Feature: Junk line
  Scenario: Has a stray line
    Given a thing
  Nonsense: what is this
```

An empty feature declares no scenarios at all:

```gherkin
Feature: Declares nothing
```
