---
name: test-driven-development
description: "Use a red-green-refactor loop to implement testable behavior changes. Use when adding a feature, fixing a bug, or refactoring code with an appropriate automated test."
---

# Test-driven development

Adapted from [obra/superpowers test-driven-development](https://github.com/obra/superpowers/tree/main/skills/test-driven-development) (MIT), for AGH projects and instructions.

1. **Read the project rules.** Find the relevant test, learn its conventions, and choose the narrowest command that exercises the behavior. Follow user and repository instructions about testing.
2. **Red.** Write one focused test for the requested behavior. Run it and confirm it fails for the expected reason. If it passes already, check whether it describes new behavior; if it errors, fix the test setup first.
3. **Green.** Make the smallest change that makes the test pass. Run the same test again. Do not weaken or remove an assertion to get a pass.
4. **Refactor.** Clean up only after the test passes. Rerun it, then run the broader checks required by the project.
5. **Report evidence.** Say which commands ran and what they showed. A test added after its implementation can still protect behavior, but it does not prove a red-green cycle; describe it accurately.

For documentation, generated files, configuration, or behavior with no sensible automated test, skip a ceremonial test. State why and use the project's relevant validation instead. Respect explicit user constraints and repository policy.

