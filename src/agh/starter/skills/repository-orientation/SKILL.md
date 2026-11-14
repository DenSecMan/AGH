---
name: repository-orientation
description: "Learn a repository before editing it: instructions, layout, entry points, conventions, and the exact build and test commands. Use at the start of coding work in a repository you have not oriented in yet."
---

# Repository orientation

Spend a few steps here so every later edit follows the repository's rules. Read; do not change anything yet.

## 1. Instructions

1. Look for agent and contributor instructions at the repository root and near the code you will touch: AGENTS.md,
   CLAUDE.md, CONTRIBUTING.md, README. Use find_files. Read each one you find.
2. These override your defaults. Note any rules about style, tests, commits, generated files and secrets.

## 2. Layout and entry points

1. list_dir the root. Identify the language and build system from manifests: pyproject.toml, setup.cfg,
   package.json, Cargo.toml, go.mod, pom.xml, build.gradle, Makefile, CMakeLists.txt.
2. Find the source root, the test directory and the entry points (CLI `main`, server app, package `__init__`, `bin`
   scripts). Note which directories are generated, vendored or runtime data; never edit those.
3. git: check status and the current branch. Note existing uncommitted changes so you never mistake them for yours
   or overwrite them.

## 3. Commands

1. Find how this repository builds, tests and lints: the instructions files, manifest scripts (`[tool.pytest]`,
   `"scripts"` in package.json, Makefile targets) and CI config (.github/workflows, .gitlab-ci.yml).
2. Prefer the narrowest test command that covers your change, such as one test file. Note the full-suite command too.
3. If cheap, run the test command once now to learn the baseline. Record failures that exist before your change.

## 4. Conventions

Read one or two files near the code you will change. Note naming, error handling, type hints, test style, and how
imports and modules are organised. Match them.

## Output

Keep a short note for yourself: instructions found, source and test paths, build and test commands, baseline result,
pre-existing changes. Use it for the rest of the task.
