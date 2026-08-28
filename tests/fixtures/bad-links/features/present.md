---
id: present
title: Present
since: spec-v1
---

# Present

## Acceptance

```gherkin
Feature: Illustration
  @id:present-paths-in-fences
  Scenario: Paths inside fences are prose, not references
    Given a spec mentioning features/imaginary.md
    Then lint does not report it
```
