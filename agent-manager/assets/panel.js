"use strict";
const $ = (id) => document.getElementById(id);
const fragment = location.hash.slice(1);
if (fragment) {
  sessionStorage.setItem("agent-manager-token", fragment);
  history.replaceState(null, "", "/");
}
const token = sessionStorage.getItem("agent-manager-token") || "";
const cards = new Map();
function el(tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) node.textContent = text;
  Object.assign(node, attrs);
  return node;
}
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: {Authorization: `Bearer ${token}`, ...(body ? {"Content-Type": "application/json"} : {})},
    ...(body ? {body: JSON.stringify(body)} : {})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求未被接受");
  return data;
}
function pre(parent, value) {
  parent.append(el("pre", typeof value === "string" ? value : JSON.stringify(value, null, 2)));
}
function option(parent, label, description, type, name, value) {
  const row = el("label", null, {className: "option"});
  const input = el("input", null, {type, name, value});
  const caption = el("span", label);
  if (description) caption.append(el("small", description, {className: "description"}));
  row.append(input, caption); parent.append(row);
  return input;
}
function nativeQuestion(parent, q, group) {
  const field = el("fieldset");
  field.append(el("legend", q.header || "问题"), el("p", q.question, {className: "question-text"}));
  parent.append(field);
  if (!q.options?.length) {
    const input = el(q.isSecret ? "input" : "textarea", null,
      q.isSecret ? {type: "password", autoComplete: "off"} : {});
    input.setAttribute("aria-label", q.question); field.append(input);
    return () => {
      if (!input.value.trim()) throw new Error("请填写问题的答案");
      return [input.value];
    };
  }
  const inputs = q.options.map((o) => option(field, o.label, o.description, "radio", group, o.label));
  let other, text;
  if (q.isOther) {
    other = option(field, "填写其他答案", "", "radio", group, "");
    text = el("input", null, {type: q.isSecret ? "password" : "text", autoComplete: "off"});
    text.setAttribute("aria-label", "其他答案"); field.append(text);
  }
  return () => {
    const selected = inputs.find((i) => i.checked);
    if (selected) return [selected.value];
    if (other?.checked && text.value.trim()) return [text.value];
    throw new Error("请选择一个答案，或填写已提供的其他答案");
  };
}
function formField(parent, name, spec, required, group) {
  const field = el("fieldset"); field.append(el("legend", (spec.title || name) + (required ? " · 必填" : " · 可选")));
  if (spec.description) field.append(el("p", spec.description));
  parent.append(field);
  let read;
  const enumOptions = (s) => s.enum ? s.enum.map((v, i) => ({value: v, label: s.enumNames?.[i] || v})) :
    (s.oneOf || s.anyOf || []).map((o) => ({value: o.const, label: o.title || o.const}));
  if (spec.type === "array") {
    const inputs = enumOptions(spec.items || {}).map((o) => option(field, o.label, "", "checkbox", group, o.value));
    read = () => {const selected = inputs.filter((i) => i.checked).map((i) => i.value); return selected.length || required ? selected : undefined;};
  } else if (spec.type === "boolean") {
    const yes = option(field, "是", "", "radio", group, "true");
    const no = option(field, "否", "", "radio", group, "false");
    read = () => {if (yes.checked) return true; if (no.checked) return false; return undefined;};
  } else if (enumOptions(spec).length) {
    const select = el("select"); select.append(el("option", "请选择", {value: ""}));
    enumOptions(spec).forEach((o) => select.append(el("option", o.label, {value: JSON.stringify(o.value)})));
    field.append(select); read = () => select.value ? JSON.parse(select.value) : undefined;
  } else {
    const numeric = spec.type === "number" || spec.type === "integer";
    const type = numeric ? "number" : ({email: "email", uri: "url", date: "date"}[spec.format] || "text");
    const input = el("input", null, {type});
    if (numeric) input.step = spec.type === "integer" ? "1" : "any";
    for (const [key, attr] of [["minimum", "min"], ["maximum", "max"], ["minLength", "minLength"], ["maxLength", "maxLength"]])
      if (spec[key] !== null && spec[key] !== undefined) input[attr] = spec[key];
    field.append(input);
    read = () => {
      if (!input.value) return undefined;
      if (!input.checkValidity()) throw new Error(`请检查 ${spec.title || name} 的格式`);
      return numeric ? Number(input.value) : input.value;
    };
  }
  return () => {
    const value = read();
    if (required && (value === undefined || (Array.isArray(value) && !value.length))) throw new Error(`请填写 ${spec.title || name}`);
    return value;
  };
}
function render(q) {
  const card = el("article");
  const top = el("div", null, {className: "top"});
  const kinds = {"item/commandExecution/requestApproval": "命令审批", "item/fileChange/requestApproval": "文件修改审批",
    "item/tool/requestUserInput": "需要你的回答", "item/permissions/requestApproval": "访问权限审批",
    "mcpServer/elicitation/request": "工具需要你的回答"};
  top.append(el("span", kinds[q.method] || "后台问题", {className: "kind"}));
  const state = el("span", "", {className: "state"}); top.append(state);
  card.append(top, el("h2", q.title), el("p", q.cwd, {className: "context"}));
  const p = q.params;
  if (p.reason || p.message) card.append(el("p", p.reason || p.message, {className: "question-text"}));
  const form = el("div"); card.append(form);
  const actions = el("div", null, {className: "actions"});
  const error = el("p", "", {className: "error", role: "alert"});
  const controls = [];
  let current = q, busy = false;
  function update(value) {
    current = value;
    state.textContent = {awaiting_confirmation: "等待你确认", answer_queued: "已提交，等待后台确认", expired: "原请求已失效"}[value.status] || value.status;
    controls.forEach((button) => button.disabled = busy || current.status !== "awaiting_confirmation");
  }
  function button(label, getResponse, primary = false) {
    const btn = el("button", label, {type: "button", className: primary ? "primary" : ""});
    btn.addEventListener("click", async () => {
      error.textContent = "";
      try {
        const response = getResponse(); busy = true; update(current);
        await api("/api/answer", {question_id: q.id, response});
        update({...current, status: "answer_queued"});
      } catch (e) { error.textContent = e.message; }
      finally { busy = false; update(current); }
    });
    controls.push(btn); actions.append(btn);
  }
  if (q.decisions.length) {
    if (p.networkApprovalContext) pre(form, p.networkApprovalContext);
    if (p.command) pre(form, p.command);
    if (p.grantRoot) form.append(el("p", "修改范围：" + p.grantRoot));
    const missingChanges = q.method === "item/fileChange/requestApproval" && !q.context?.changes;
    if (q.context?.changes) pre(form, q.context.changes);
    if (missingChanges) form.append(el("p", "尚未取得具体文件差异，面板暂不提供允许按钮。可保留等待，或在入口核对原始改动后用 answer 明确答复。"));
    const labels = {accept: "仅本次允许", acceptForSession: "允许此会话", decline: "拒绝", cancel: "取消本次执行"};
    q.decisions.forEach((decision) => {
      if (missingChanges && ["accept", "acceptForSession"].includes(decision)) return;
      if (typeof decision === "object") {
        const label = decision.acceptWithExecpolicyAmendment ? "允许并保存此命令规则" : "应用下方规则决定";
        pre(form, decision); button(label, () => ({decision}));
      } else button(labels[decision] || decision, () => ({decision}), decision === "accept");
    });
  } else if (q.method === "item/tool/requestUserInput") {
    const reads = (p.questions || []).map((question, i) => [question.id, nativeQuestion(form, question, q.id + "-" + i)]);
    button("提交回答", () => ({answers: Object.fromEntries(reads.map(([id, read]) => [id, {answers: read()}]))}), true);
  } else if (q.method === "item/permissions/requestApproval") {
    const requested = p.permissions || {}, grants = [];
    if (requested.network?.enabled) {
      const input = option(form, "允许网络访问", "仅本轮生效", "checkbox", q.id + "network", "network");
      grants.push(() => input.checked ? ["network", {enabled: true}] : null);
    }
    const fs = requested.fileSystem || {}, reads = [];
    for (const key of ["entries", "read", "write"]) {
      const entries = fs[key] || [];
      const inputs = entries.map((entry, i) => option(form, typeof entry === "string" ? entry : JSON.stringify(entry), key, "checkbox", q.id + key, String(i)));
      if (entries.length) reads.push(() => [key, entries.filter((_, i) => inputs[i].checked)]);
    }
    if (reads.length) grants.push(() => ["fileSystem", Object.fromEntries(reads.map((read) => read()).filter(([, entries]) => entries.length))]);
    button("提交选中的权限（仅本轮）", () => ({permissions: Object.fromEntries(grants.map((read) => read()).filter(Boolean)), scope: "turn"}), true);
    button("全部拒绝", () => ({permissions: {}, scope: "turn"}));
  } else if (q.method === "mcpServer/elicitation/request") {
    if (p.mode === "url") {
      let usableUrl = false;
      try {
        const url = new URL(p.url);
        if (["https:", "http:"].includes(url.protocol)) {
          usableUrl = true;
          form.append(el("a", "打开工具提供的验证页面", {href: url.href, target: "_blank", rel: "noopener noreferrer", className: "link"}));
        }
      } catch (_) { /* Keep malformed URLs as plain text in request details. */ }
      if (usableUrl) button("我已处理验证页面，继续", () => ({action: "accept", content: null}), true);
    } else if (q.form_supported) {
      const required = new Set(p.requestedSchema.required || []);
      const reads = Object.entries(p.requestedSchema.properties).map(([name, spec], i) => [name, formField(form, name, spec, required.has(name), q.id + "mcp" + i)]);
      button("提交回答", () => ({action: "accept", content: Object.fromEntries(reads.map(([name, read]) => [name, read()]).filter(([, value]) => value !== undefined))}), true);
    } else form.append(el("p", "此表单格式尚不支持。请在入口核对原请求，或选择拒绝/取消。"));
    button("拒绝", () => ({action: "decline", content: null}));
    button("取消", () => ({action: "cancel", content: null}));
  } else form.append(el("p", "此问题类型尚不支持，请在入口核对原请求。"));
  card.append(error, actions);
  const details = el("details"); details.append(el("summary", "会话与原始请求详情"));
  pre(details, {thread: q.thread_id, question: q.id, request: p}); card.append(details);
  $("questions").append(card); update(q);
  return {node: card, update};
}
async function refresh() {
  try {
    const data = await api("/api/questions");
    $("project").textContent = data.project;
    $("connection").textContent = "本机连接正常";
    $("notice").textContent = "";
    const ids = new Set(data.questions.map((q) => q.id));
    for (const [id, card] of cards) if (!ids.has(id)) {card.node.remove(); cards.delete(id);}
    for (const q of data.questions) {
      if (cards.has(q.id)) cards.get(q.id).update(q);
      else cards.set(q.id, render(q));
    }
    $("empty").hidden = data.questions.length > 0;
  } catch (e) {
    $("connection").textContent = "连接未确认";
    $("notice").textContent = e.message || "请用 panel 命令重新打开面板。";
  } finally { setTimeout(refresh, 1500); }
}
refresh();
