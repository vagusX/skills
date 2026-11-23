# skills

- [ambition](ambition/SKILL.md) — rewrite polite action asks into ambitious outcomes before executing
- [eli18](eli18/SKILL.md)：按高中生能理解的程度，用 HTML 图解解释主题、原理和具体例子。
- [grill-me-less](grill-me-less/SKILL.md) — ask at most three questions about the current task; explicit invocation only
- [continue-in-new-thread](continue-in-new-thread/SKILL.md) — continue in a new conversation with only the context needed; explicit invocation only
- [agent-scratch](agent-scratch/SKILL.md)：统一管理各 Agent 临时目录（优先 `~/agent-scratch/<agent>/<session-id>/`，沙箱无权写 `~` 时回退 `/tmp/agent-scratch/<agent>/<session-id>/`，附 `readme.txt` 与盘点/清理脚本）。

### `agent-scratch` 必配说明

因为 skill 是按需加载的，仅安装本 skill 不会阻止 Agent 随手往 `/tmp` 写文件。**须在全局 `AGENTS.md`（以及独立的 `CLAUDE.md` 等常驻规则文件）中加一行**：

```markdown
- 临时文件禁止散落写入 `/tmp`：优先写入 `~/agent-scratch/<agent>/<session-id>/`（沙箱无权写 `~` 时回退到 `/tmp/agent-scratch/<agent>/<session-id>/`），首次创建时写入 `readme.txt` 注明创建时间及简短用途（≤50字）；盘点或清理走 `agent-scratch` skill。
```
