---
id: mentions-a-rule
title: The word Rule where it is not a Rule
since: spec-v1
---

# The word Rule where it is not a Rule

The negative control, and the lesson `GH007` and `GH009` both paid for: the
rule is keyed on the parsed node, not on the token. A `Rule:` inside a
docstring is literal text and one inside a step's text is prose, so neither is
a Rule and neither may be faulted. This file is clean apart from the missing
frontmatter nothing here checks.

## Acceptance

```gherkin
Feature: Documentation
  @id:rules-in-a-docstring-is-not-a-rule
  Scenario: A docstring quoting a Rule
    Given the policy text
      """
Rule: Only admins may delete
      """
    When it is rendered
    Then the quoted line survives verbatim

  @id:rules-in-step-text-is-not-a-rule
  Scenario: A step naming a Rule
    Given a spec section that says "Rule: Only admins may delete"
    Then the section is prose, not a Gherkin Rule
```
