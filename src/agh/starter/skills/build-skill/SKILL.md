---
name: build-skill
description: "How to write a new AGH skill. Use before calling create_skill."
---

# Build a skill

A skill is a procedure loaded on demand. Only its description sits in context until load_skill is called.

- Description: say what it does and when to use it, with words a user would use. It is the trigger.
- Body: numbered steps with exact commands and the pitfalls. Skip what any capable agent already knows.
- Long reference material goes in `resources`, loaded with load_skill(name, resource="path").
