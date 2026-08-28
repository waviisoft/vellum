---
id: empty-rule
title: A Rule holding nothing
since: spec-v1
---

# A Rule holding nothing

`spec/decisions/2026-08-28-no-rules.md` records that a `Rule:` with no
scenarios is moot as an unrunnable-class member, because "the construct fails
lint before its emptiness matters". This is that case: `GH010` fires on the
`Rule:` itself, and the block draws `GH002` besides, because once the Rule is
not walked the fence really does declare no scenarios.

## Acceptance

```gherkin
Feature: Retention
  Rule: Records older than a year are purged
```
