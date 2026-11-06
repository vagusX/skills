---
name: continue-in-new-thread
description: Continue the current task or a specified topic in a new conversation. Use only when the user explicitly invokes continue-in-new-thread.
disable-model-invocation: true
---

# Continue in a new thread

Move the current task or the topic specified by the user to a new conversation. Carry over only the context needed to continue: keep the handoff brief for simple tasks and include enough detail for complex ones. This may include the goal, progress, key decisions, constraints, relevant materials, and next steps. Stay within the user's existing authorization and do not repeat completed actions. Once the new conversation has been created, provide a way to open it. If you cannot create it, provide a handoff the user can copy into a new conversation and clearly state that no new conversation has been created.
