"""Human answers to live native requests; shared by the CLI and local panel."""

from datetime import date, datetime
from email.headerregistry import Address
from email.errors import HeaderParseError
import json
import math
import time
from urllib.parse import urlsplit

from .codex import process_alive
from .store import decode, encode


COMMAND = "item/commandExecution/requestApproval"
FILE = "item/fileChange/requestApproval"
INPUT = "item/tool/requestUserInput"
PERMISSIONS = "item/permissions/requestApproval"
ELICITATION = "mcpServer/elicitation/request"


def live_request(store, job):
    chat = store.one("SELECT hold,closed FROM chats WHERE id=?", (job["chat_id"],))
    if not chat or chat["hold"] or chat["closed"]:
        return False
    try:
        owner = json.loads((store.directory / "daemon.json").read_text())
        if not process_alive(owner["pid"], owner["started"]):
            return False
    except (OSError, ValueError, KeyError):
        return False
    request = decode(job["payload"], {})
    params = request.get("params", {})
    run = store.one("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (job["parent_id"],))
    if not run or run["state"] not in {"prepared", "running"}:
        return False
    if (request.get("server_pid"), request.get("server_start")) != (run["server_pid"], run["server_start"]):
        return False
    if params.get("threadId") != run["thread_id"]:
        return False
    if params.get("turnId") and run["turn_id"] and params["turnId"] != run["turn_id"]:
        return False
    return process_alive(request.get("server_pid"), request.get("server_start"))


def choices(method, params):
    if method == COMMAND:
        return params.get("availableDecisions") if params.get("availableDecisions") is not None else ["accept", "acceptForSession", "decline", "cancel"]
    if method == FILE:
        return ["accept", "acceptForSession", "decline", "cancel"]
    return []


def validate_form(schema, value):
    """The flat primitive form schema accepted by MCP elicitation."""
    if not form_supported(schema):
        raise ValueError("unsupported form schema; keep pending or decline/cancel")
    if not isinstance(value, dict):
        raise ValueError("form content must be an object")
    properties = schema.get("properties", {})
    if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
        raise ValueError("form fields do not match the requested schema")
    for key, item in value.items():
        spec = properties[key]
        kind = spec.get("type", "string")
        if kind == "array":
            if not isinstance(item, list) or any(not isinstance(x, str) for x in item):
                raise ValueError("array answers must contain strings")
            options = spec.get("items", {})
            allowed = options.get("enum", [x.get("const") for x in options.get("anyOf", [])])
            if any(x not in allowed for x in item) or len(set(item)) != len(item):
                raise ValueError("unknown or duplicate array choice")
            if len(item) < spec.get("minItems", 0) or len(item) > spec.get("maxItems", len(item)):
                raise ValueError("array answer has the wrong number of choices")
        elif kind == "boolean":
            if not isinstance(item, bool):
                raise ValueError("expected a boolean answer")
        elif kind in {"number", "integer"}:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or (kind == "integer" and int(item) != item):
                raise ValueError("expected a numeric answer")
            if item < spec.get("minimum", item) or item > spec.get("maximum", item):
                raise ValueError("numeric answer is outside the requested bounds")
        elif kind == "string":
            if not isinstance(item, str):
                raise ValueError("expected a text answer")
            allowed = spec.get("enum")
            if allowed is None and spec.get("oneOf"):
                allowed = [x.get("const") for x in spec["oneOf"]]
            if allowed is not None and item not in allowed:
                raise ValueError("unknown form choice")
            if len(item) < spec.get("minLength", 0) or len(item) > spec.get("maxLength", len(item)):
                raise ValueError("text answer has the wrong length")
            try:
                if spec.get("format") == "email":
                    parsed = Address(addr_spec=item)
                    if not parsed.domain:
                        raise ValueError("email requires a domain")
                elif spec.get("format") == "uri":
                    if not urlsplit(item).scheme or any(c.isspace() for c in item):
                        raise ValueError("URI requires a scheme")
                elif spec.get("format") == "date":
                    date.fromisoformat(item)
                elif spec.get("format") == "date-time":
                    parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("date-time requires a timezone")
            except (ValueError, IndexError, HeaderParseError) as error:
                raise ValueError("text answer does not match the requested format") from error
        else:
            raise ValueError("unsupported form field type; use the original client")


def form_supported(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return False
    if set(schema) - {"type", "properties", "required", "title", "description", "$schema", "additionalProperties"}:
        return False
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(k, str) or k not in schema["properties"] for k in required):
        return False
    def enum_supported(spec):
        if "enum" in spec:
            values = spec["enum"]
            return isinstance(values, list) and bool(values) and all(isinstance(v, str) for v in values)
        values = spec.get("oneOf", spec.get("anyOf"))
        return isinstance(values, list) and bool(values) and all(isinstance(v, dict) and isinstance(v.get("const"), str) for v in values)
    allowed = {"type", "title", "description", "default", "minLength", "maxLength", "format",
               "enum", "enumNames", "oneOf", "items", "minItems", "maxItems", "minimum", "maximum"}
    for spec in schema["properties"].values():
        if not isinstance(spec, dict) or set(spec) - allowed or not isinstance(spec.get("type"), str) or spec["type"] not in {"string", "number", "integer", "boolean", "array"}:
            return False
        if spec.get("format") is not None and (not isinstance(spec["format"], str) or spec["format"] not in {"email", "uri", "date", "date-time"}):
            return False
        if any(key in spec for key in ("enum", "oneOf")) and not enum_supported(spec):
            return False
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in spec and (type(spec[key]) is not int or spec[key] < 0):
                return False
        for key in ("minimum", "maximum"):
            if key in spec and (isinstance(spec[key], bool) or not isinstance(spec[key], (int, float)) or not math.isfinite(spec[key])):
                return False
        if spec["type"] == "array":
            item = spec.get("items", {})
            if not isinstance(item, dict) or set(item) - {"type", "enum", "enumNames", "anyOf"} or not enum_supported(item):
                return False
    return True


def validate_response(request, response):
    if not isinstance(response, dict):
        raise ValueError("native response must be an object")
    method, params = request["method"], request["params"]
    if method in {COMMAND, FILE}:
        if set(response) != {"decision"} or response["decision"] not in choices(method, params):
            raise ValueError("decision is not offered by this request")
    elif method == INPUT:
        questions = {q["id"]: q for q in params["questions"]}
        answers = response.get("answers")
        if set(response) != {"answers"} or not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("answer every original question by its ID")
        for key, answer in answers.items():
            values = answer.get("answers") if isinstance(answer, dict) else None
            if not isinstance(answer, dict) or set(answer) != {"answers"} or not isinstance(values, list) or not values or any(not isinstance(x, str) or not x.strip() for x in values):
                raise ValueError("answers must be nonempty strings")
            question = questions[key]
            if question.get("options") and not question.get("isOther"):
                allowed = {o["label"] for o in question["options"]}
                if any(x not in allowed for x in values):
                    raise ValueError("answer is not an offered option")
    elif method == PERMISSIONS:
        requested = params.get("permissions", {})
        granted = response.get("permissions")
        if set(response) - {"permissions", "scope"} or not isinstance(granted, dict) or response.get("scope", "turn") not in {"turn", "session"}:
            raise ValueError("invalid permission response")
        if set(granted) - set(requested):
            raise ValueError("cannot grant unrequested permissions")
        for section, values in granted.items():
            original = requested.get(section) or {}
            if not isinstance(values, dict) or set(values) - set(original):
                raise ValueError("cannot grant an unrequested permission field")
            for key, value in values.items():
                if isinstance(original[key], bool):
                    if not isinstance(value, bool) or (value and not original[key]):
                        raise ValueError("cannot expand the requested boolean permission")
                elif isinstance(original[key], list):
                    if not isinstance(value, list) or any(x not in original[key] for x in value):
                        raise ValueError("cannot grant an unrequested path")
                elif value != original[key] and not (value is False and original[key] is True):
                    raise ValueError("cannot expand the requested permission")
    elif method == ELICITATION:
        action = response.get("action")
        if set(response) != {"action", "content"} or action not in {"accept", "decline", "cancel"}:
            raise ValueError("invalid elicitation response")
        if action == "accept" and params.get("mode", "form") in {"form", "openai/form"}:
            validate_form(params["requestedSchema"], response["content"])
        elif response["content"] is not None:
            raise ValueError("this elicitation response must have null content")
    else:
        raise ValueError("unsupported native question type")


def queue_answer(store, question_id, response):
    with store.transaction():
        job = store.one("SELECT * FROM jobs WHERE id=? AND kind='question'", (question_id,))
        if not job or job["status"] != "pending" or job["result_ref"] is not None:
            raise ValueError("question has already been answered or is no longer pending")
        if not live_request(store, job):
            raise ValueError("original native request is no longer live; do not reuse this answer")
        validate_response(decode(job["payload"]), response)
        store.db.execute("UPDATE jobs SET result_ref=?,updated_at=? WHERE id=? AND status='pending' AND result_ref IS NULL",
                         (encode(response), time.time(), question_id))
    return {"answer_queued": True, "question_id": question_id}


def answer_receipt(store, question_id):
    job = store.one("SELECT * FROM jobs WHERE id=? AND kind='question'", (question_id,))
    if not job:
        raise ValueError("unknown question")
    ref = decode(job["native_ref"], {})
    confirmed = ref.get("resolved_at") and ref.get("answer_sent", job["result_ref"] is not None)
    phase = ("resolved" if confirmed else "expired" if job["status"] == "done"
             else "unknown" if job["status"] == "unknown" else "sent" if job["status"] == "inflight"
             else "answer_queued" if job["result_ref"] is not None else "pending")
    return {"question_id": question_id, "phase": phase, "native_ref": ref, "error": job["error"]}


def pending_questions(store):
    result = []
    for job in store.all("SELECT j.*,c.title,c.thread_id,c.cwd FROM jobs j JOIN chats c ON c.id=j.chat_id WHERE j.kind='question' AND j.status!='done' ORDER BY j.created_at,j.id"):
        request = decode(job["payload"], {})
        live = live_request(store, job) if request else False
        result.append({"id": job["id"], "title": job["title"], "thread_id": job["thread_id"],
                       "cwd": job["cwd"], "method": request.get("method"), "params": request.get("params", {}),
                       "context": request.get("context"),
                       "form_supported": form_supported(request.get("params", {}).get("requestedSchema")),
                       "status": "awaiting_confirmation" if live and job["status"] == "pending" and job["result_ref"] is None else "answer_queued" if live and job["result_ref"] is not None else "expired",
                       "decisions": choices(request.get("method"), request.get("params", {}))})
    return result
