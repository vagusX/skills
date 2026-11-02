---
name: ambition
description: >-
  Use when assigning work to an agent or writing a prompt that sounds like a
  polite action (“帮我写个 X”, “改一下 Y”, “做一份 PPT”) — rewrite it into an ambitious
  outcome before executing.
---
# Ambition

Rewrite a polite, action-level ask into an ambitious outcome **before** you run tools or dispatch work. Do not execute the original ask until this rewrite is done (or the user explicitly keeps the polite version).

Inspired by Felix Rieseberg (Claude Code desktop): people under-use AI because the **goal is too polite**, not because the model is weak. Verification without self-grading stays in `prove-it-works`; this skill only raises the ask.

## When to skip

- The ask is already an outcome with a hard bar (latency, pass rate, decision, invariant).
- The user said to keep the narrow scope (“只要改这一行”, “先别抬目标”).
- Pure lookup / paste / one-shot formatting with no “success” beyond the artifact.

## Steps

### 1. Catch the polite ask

Mark it as polite if it is mostly a **noun/deliverable** or a **local action**:

- “写个报告 / PPT / PR 描述”
- “帮我修这个 bug / 改顺这段”
- “总结一下 / 润色一下”

### 2. Noun → verb (expose the real goal)

Rewrite the deliverable into what should **happen** for whom, under what constraint.

| Polite (noun) | Ambition (verb / outcome) |
| --- | --- |
| 做一份季度汇报 PPT | 在 30 分钟内，让一群忙碌的人信服前因后果，并在情况不明时仍能拍板 |
| 帮我修这个崩溃 | 让应用保持不崩溃（或：崩溃率低于 X，可观测） |
| 写个性能优化方案 | 把 P95 从 10s 压到可感知的「瞬间」（先定硬数字） |

If finishing the artifact would still require a **meeting to decide what it meant**, raise one more layer: you wanted the decision / invariant, not the file.

### 3. Set an “unreasonable” bar

Take the obvious improvement and **multiply the ambition** (example pattern: 10s → not 5s, but 0.2s).

- Write the hard target in one line (number, invariant, or decision).
- If the model says it is impossible: reply that the problem is solvable and try again with a different approach — do not immediately retreat to the polite bar unless the user overrides.

### 4. Depth: tools and raw materials, not “think like an expert”

Do **not** only say “像专家一样思考” (that mostly changes vocabulary).

Into the rewritten ask, name:

- **Raw materials** experts would open (logs, traces, schemas, primary papers, prod metrics — not second-hand summaries).
- **Tools / methods** they would use (profiler, DB + stats model, eval harness — not a Markdown table pretending to be analysis).

### 5. Hand off

Output, then proceed with the rewritten ask:

1. **One-line ambitious goal** (verb + bar).
2. **Materials & tools** to use.
3. **How we will prove it** — point to `prove-it-works` (run it, new holdout data, seek disconfirming evidence). Never accept the model grading itself.

## Output template

```text
原目标：…
抬高后：…
材料/工具：…
验收：按 prove-it-works（…）
```

## Anti-patterns

- Raising scope without a bar (“做得更好一点”).
- Swapping jargon while keeping the same method.
- Letting the model self-score as done.
- Splitting this into separate “goal” and “expert depth” skills and only running one half.
