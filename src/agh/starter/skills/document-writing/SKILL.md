---
name: document-writing
description: "Write or revise a document for readers: report, proposal, spec, design or decision doc, README, guide, how-to, announcement. Use when the user asks you to draft, write or improve a document."
---

# Document writing

Adapted from Anthropic's doc-coauthoring skill.

## 1. Purpose and audience

Know three things before drafting: who reads it, what they must know or do afterwards, and any required format or
length. Infer them from the request and files where you can. Ask at most one round of short questions for the rest,
then draft. If the user says "just write it", state your assumptions at the top of your reply and proceed.

## 2. Gather material

Read the files, notes and earlier documents the user points to. Delegate external facts to `researcher`. List what is
still unknown; do not invent facts, numbers or quotes.

## 3. Structure

1. Lead with the conclusion, decision or request. Background comes after.
2. Outline the section headings and one line each on what they cover. For a long or important doc, show the user the
   outline before drafting.
3. Pick the form that fits: steps for procedures, a table for comparisons, prose for reasoning.

## 4. Draft

- Write for the named reader. Define terms they may not know. Cut what they already know.
- One idea per paragraph. Short sentences, active voice, concrete examples.
- Use one name for each thing throughout.
- Mark gaps clearly, for example `TODO: confirm launch date`, rather than guessing.

## 5. Check

Before handing over, reread as the reader:

1. Can they answer: what is this, why does it matter to me, what do I do next?
2. Are names, numbers, dates and links consistent across sections?
3. Does every section serve the purpose? Cut the rest.
4. Does it meet the requested format and length?

Then report where the file is, the assumptions you made, and the open TODOs.
