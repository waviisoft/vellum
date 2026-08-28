---
id: absorbed
title: A second Feature the parser swallows
since: spec-v1
---

# A second Feature the parser swallows

The parser refuses a second `Feature:` only where it reaches one as a
declaration. Reached where free text is legal it absorbs the line as prose, so
the block parses one Feature short, with the second Feature's scenarios
re-parented onto the first and its name gone. Both blocks below do that, and
both hold two Features.

## Into a Feature description

```gherkin
Feature: Has only a narrative

  This narrative is where the next Feature line disappears to.

Feature: Swallowed by a description
  @id:absorbed-into-feature-description
  Scenario: Still belongs to the second Feature
    Given a thing
    Then it works
```

## Into a Scenario description

```gherkin
Feature: Has a scenario with no steps yet
  @id:absorbed-host-scenario
  Scenario: Carries a narrative and no steps
    this narrative is where the next Feature line disappears to

Feature: Swallowed by a scenario description
  @id:absorbed-into-scenario-description
  Scenario: Also still belongs to the second Feature
    Given a thing
    Then it works
```
