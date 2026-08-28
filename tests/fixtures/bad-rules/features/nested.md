---
id: nested
title: Scenarios nested under a Rule
since: spec-v1
---

# Scenarios nested under a Rule

The finding from waviisoft/vellum-intent#16: a stock Cucumber runner executes
all three scenarios below, and extraction describes only the first. The two
under the `Rule:` are dropped in silence, which is the class `GH009` exists to
close and the reason the ban was chosen.

## Acceptance

```gherkin
Feature: Deletion
  @id:rules-direct-child
  Scenario: A direct child of the Feature
    Given a signed-in user
    Then the page renders

  Rule: Only admins may delete
    @id:rules-nested-example
    Example: An admin deletes
      Given an admin
      When they delete a record
      Then the record is gone

    @id:rules-nested-scenario
    Scenario: A normal user may not delete
      Given a normal user
      When they delete a record
      Then the delete is refused
```
