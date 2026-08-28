---
id: absorbs-what-follows
title: A Rule swallows every scenario after it
since: spec-v1
---

# A Rule swallows every scenario after it

Gherkin is not indentation-sensitive, so a `Rule:` does not hold "the indented
block below it" — it holds **every scenario until the next Rule or the end of
the Feature**, however the file is laid out. One stray `Rule:` line above a
Feature's existing scenarios therefore moves all of them out of the suite at
once, silently. Measured on the real tree during the wave that added `GH010`:
inserting one `Rule:` line into `features/repo-topology.md` took its scenario
out of the extraction along with the smuggled one.

The scenario below reads as a direct child of the Feature and is not one.

## Acceptance

```gherkin
Feature: Retries
  Rule: A lapsed lease returns the item to the queue

  @id:rules-absorbed-by-a-rule-above
  Scenario: Looks like a direct child, is not one
    Given a claimed work item
    When its lease lapses
    Then the item returns to the queue
```
