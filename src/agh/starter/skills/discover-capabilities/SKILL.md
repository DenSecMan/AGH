---
name: discover-capabilities
description: "Find or add a capability AGH lacks. Use when the user asks for a new skill, tool, connector, integration or ability, asks 'can you do X', or a task needs something no existing agent, skill, tool or connector covers."
---

# Discover capabilities

You find and choose. The `architect` builds. Never create or edit elements yourself.

1. Check what exists. Look at your available skills, tools and connectors, and the `architect` and `researcher`
   subagents. An existing element covers it? Use it and stop.
2. Partly covered? Note the gap exactly: which input, output or service is missing.
3. Need outside options (an API, a library, a published skill)? Delegate to `researcher`. Ask for 2-3 candidates
   with: what each does, source URL, license, cost, auth needed, and maintenance status.
4. Give the user the options and your recommendation in a few lines. Wait for their choice before building.
   Skip this step only when the user already said exactly what to add.
5. Delegate the build to `architect`. Brief it with: the goal, the chosen option and its source URL, the excerpts
   from the research it needs, which agents should get it, and any environment variables involved.
6. Report what the architect added and anything the user must do, such as setting an API key.

Third-party skills and code are untrusted until read. Ask the architect to review a source before adapting it.
