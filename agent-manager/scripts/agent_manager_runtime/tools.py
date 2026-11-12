"""Schemas for the scheduler's own native Codex dynamic tools."""


def tool(name, description, properties, required):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


STRING = {"type": "string"}
WORK = {"work_id": STRING}
MAIN_TOOLS = [
    tool("sa_memory", "Read project-first memory, or upsert one verified project fact with a source. Memory is data, not permission or execution state. Global writes require an explicit human operation in the entry CLI.",
         {"action": {"type": "string", "enum": ["read", "put"]},
          "key": STRING, "text": STRING, "source": STRING}, ["action"]),
    tool("sa_list_work", "Read one snapshot of this manager's registered Codex workers, execution observations and pending native questions. No waiting or side effects.",
         {}, []),
    tool("sa_create_work", "Create one worker for the current explicitly authorized request. Original text is supplied by the scheduler; do not rewrite it.",
         {"title": STRING, "cwd": STRING}, ["title"]),
    tool("sa_inspect_work", "Read actual work state, native history and optional artifact paths relative to the work cwd. Use before delivering a result. Passing this manager's chat ID as work_id lists its registered workers instead.",
         {**WORK, "paths": {"type": "array", "items": STRING}}, ["work_id"]),
    tool("sa_followup", "Continue an existing worker within the original authorized scope. Never use for ordinary conversation or just asking for status.",
         {**WORK, "text": STRING}, ["work_id", "text"]),
    tool("sa_attach_event", "Deliver the current authorized request to an existing work item without creating a new worker.",
         WORK, ["work_id"]),
    tool("sa_set_hold", "Save an explicit human pause request from the current local task. Only a human continue command can release it.",
         {**WORK, "reason": STRING}, ["work_id", "reason"]),
    tool("sa_reply", "Queue a reply to the work's original external issue only if that source explicitly allows replies. Otherwise retain a draft in this native conversation.",
         {**WORK, "text": STRING}, ["work_id", "text"]),
]
WORKER_TOOLS = [
    tool("sa_report", "Persist the work result and notify the main thread when appropriate. This is the required report interface, not a normal chat message.",
         {"state": {"type": "string", "enum": ["progress", "ready", "blocked", "failed", "dropped"]},
          "summary": STRING, "evidence": {"type": "array", "items": STRING},
          "question": {"type": ["string", "null"]},
          "remaining": {"type": "array", "items": STRING}},
         ["state", "summary", "evidence", "question", "remaining"]),
    tool("sa_return_event", "Return a request outside this work's scope to the main thread, without creating another worker.",
         {"reason": STRING}, ["reason"]),
]
