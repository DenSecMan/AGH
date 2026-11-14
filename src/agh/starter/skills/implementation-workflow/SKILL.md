---
name: implementation-workflow
description: "Carry a code change from request to verified result: clarify the behaviour, inspect the code, make focused edits, verify, and report gaps. Use for any feature, bug fix or refactor."
---

# Implementation workflow

## 1. Clarify the behaviour

1. Restate the requested behaviour in one or two sentences, including how you will know it works.
2. Ask the user only if the requirement is genuinely ambiguous and the code cannot settle it. Otherwise state your
   assumption and proceed. Do not stop for approval between planning steps.

## 2. Inspect

1. Use search_files and find_files to locate the code involved, then read_file the relevant ranges.
2. Find existing tests for this area and any similar feature you can follow.
3. For a bug, load systematic-debugging and reproduce it before changing code.

## 3. Plan

For three or more steps, write the plan with update_todos: the files to change, the tests to add or update, and the
command that verifies it. Keep it updated as you go.

## 4. Change

1. Make the smallest change that delivers the behaviour. Use edit_file for edits; read the exact text first so the
   replacement matches. Use write_file only for new files or full rewrites.
2. Follow the conventions you found in orientation. No unrelated refactors, renames or formatting.
3. Add or update a focused test for the behaviour where the repository has tests. For a bug fix, the test should fail
   without the fix.
4. A refactor must not change behaviour: run the tests before and after.
5. Before using a library, confirm the repository already depends on it (its manifest, or imports in neighbouring
   files). Add a new dependency only when the request needs it, and say so in the report.
6. Add comments only where the logic is not obvious from the code. Do not add license or copyright headers.

## 5. Verify

Run the narrow test command, then the broader suite if it is affordable. If something fails, read the output and fix
the cause; do not weaken or skip tests to make them pass. Then load verification-before-completion.

## 6. Report

List the files changed, the commands run with their results, and remaining gaps: anything untested, assumptions made,
or pre-existing failures you left alone.
