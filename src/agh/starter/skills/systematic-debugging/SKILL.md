---
name: systematic-debugging
description: "Find the root cause before fixing. Use for any bug, test failure, crash, error message, failing build, broken config, provider or connector error, or unexpected behavior, before proposing a fix."
---

# Systematic debugging

Find the root cause before you change anything. A fix for a symptom is not a fix.
Adapted from obra/superpowers systematic-debugging (MIT).

## 1. Investigate

1. Read the whole error and stack trace. Note file paths, line numbers and error codes.
2. Reproduce it. Write down the exact steps or command. Not reproducible? Gather more data; do not guess.
3. Check what changed: recent edits, `git diff`, new dependencies, config, environment variables, provider settings.
4. Several components (agent, tool, connector, provider, shell)? Log what enters and leaves each boundary once,
   to find which one breaks. Then investigate only that one.
5. Bad value deep in a call chain? Trace it back to where it originates. Fix it there.

## 2. Compare

1. Find similar code or config that works.
2. List every difference between working and broken, however small.
3. Following a reference or API doc? Read the relevant part completely.

## 3. Test one hypothesis

1. State it: "X is the root cause because Y."
2. Make the smallest change that tests it. Change one thing at a time.
3. Wrong? Undo it, and form a new hypothesis. Do not stack fixes.
4. Stuck? Say what you do not understand. Ask the user, or delegate research to `researcher`.

## 4. Fix

1. Write a failing test or a one-off reproduction script first, where practical.
2. Make one fix for the root cause. No unrelated cleanups.
3. Verify: the reproduction passes, other tests still pass, and the original symptom is gone.
4. Three fixes failed? Stop. The design is likely wrong. Report what you found and ask the user before trying more.

## Stop signs

Return to step 1 if you catch yourself thinking: "quick fix for now", "just try X and see", "it's probably X",
"several changes then run the tests", or "one more attempt" after two failures.

Truly environmental, timing-dependent or external cause? Record what you checked, add handling (retry, timeout,
clear error message), and say so in your report.
