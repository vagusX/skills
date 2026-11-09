# 工作记录

这是本 skill 自定义的本地记录，不是 Codex 官方会话格式。Codex 继续保存会话正文；这里只存原始请求、会话引用和管理所需的事实。

## 位置与写入

使用主 chat 的固定绝对工作目录作为记录根目录；在 Git 仓库中通常取当前 checkout 根目录。记录位置为：

```text
<主 chat 的项目目录>/.super-agent/<主 chat 的 threadId>/state.json
```

主 chat 标识必须来自可信宿主元数据。不能取得真实 ID 时不创建冒名记录；说明所缺信息。记录根目录选定后，不随 worker 的工作目录或 worktree 改变。

主 chat 是唯一写入者，worker 和内部 subagents 都不直接修改。不同主 chat 各有自己的文件。读取已有记录时核对 `ownerThreadId` 与工作目录；不是自己的记录时只读，明确的人类接管要求才可转移写入职责，并先确认原管理者不会继续写。无法确认单一写入者时不并发修改。

把 `.super-agent/` 排除出 Git：先检查现有忽略规则；需要补充时写入 `git rev-parse --git-path info/exclude` 所指的本地 exclude 文件，返回的相对路径按执行该 Git 命令的目录解析。保留已有内容，不重复追加。非 Git 目录无须这一操作。不写进 skill 目录、安装缓存或系统临时目录。

保存时先读最新文件，只更新本次变化；先写同目录临时文件并解析检查 JSON，再原子替换 `state.json`。写后重新读取，核对 owner、原请求、会话标识和其他工作条目仍在。写入失败就暂停后续派工，不在缺少记录时继续创建会话。损坏文件应保留，不能用空文件覆盖来“修复”。

## 最小格式

下列尖括号内容是待填值，不能当真实事实保存。字段均由本文件定义；其中 `threadId`、`hostId`、`clientThreadId` 与等待 cursor 的值必须来自原生工具。

```json
{
  "ownerThreadId": "<真实主 chat ID>",
  "directory": "<主 chat 的固定绝对项目目录>",
  "work": [
    {
      "id": "<主 chat 生成且不复用的工作 UUID>",
      "request": "<原始人类请求及可用来源引用>",
      "constraints": [],
      "dispatch": "pending",
      "title": "<拟创建的可读标题；成功后保留工具返回的实际标题>",
      "threadId": null,
      "hostId": null,
      "clientThreadId": null,
      "directory": null,
      "waitCursor": null,
      "hold": null,
      "report": null
    }
  ]
}
```

`constraints` 保留人的已有约束和后续修正及其来源；不放主 chat 自拟的技术方案。工作条目的 `directory` 只记录工具或 worker 已确认的实际路径，未确认时保留 null。

`dispatch` 只描述派工情况，不描述工作是否完成：

| 值 | 含义 |
| --- | --- |
| pending | 已记录，尚未发起创建 |
| creating | 即将调用创建工具，或只得到 clientThreadId、仍在准备 |
| linked | 已确认真实 threadId，可通过原生工具读取 |
| uncertain | 创建请求失败或超时，无法确定是否已经创建 |
| failed | 已有明确失败结果，且已核实没有遗留会话 |

创建调用之前先保存 creating。返回后先保存真实结果，再处理其他工作。恢复时 pending 可以按原授权派发；creating 和 uncertain 先核对已有 chat；linked 继续读取原会话。原生工具不提供事务性“创建并写文件”，这些记录只能帮助恢复核对，不能宣称 exactly-once。

`hold` 为 null 或人的停止/等待要求原文、来源及解除条件。它独立于 dispatch 和 worker 报告。若人要求“等我确认”，新的线索、ready 或失败都不能替代确认；若人明确要求“等 A 有可审查结果后再做 B”，核对该条件确已满足后可以继续 B，无须人再说一次继续。不能把一个条件的满足解释成其他工作的新增授权。

需要保留原生状态观察时，可以增加 `observation`，记录工具实际返回的 `status` 与查询时间 `observedAt`。它只表示上次观察，不覆盖 hold 或业务报告；active 不能证明已停止，idle 也不能证明工作完成。

`report` 为 null 或从 worker 实际报告中取得的对象：

```json
{
  "state": "ready",
  "summary": "<实际结果>",
  "evidence": ["<产物位置及实际检查凭据>"],
  "question": null,
  "remaining": []
}
```

evidence 保留 worker 的实际报告内容，可以是列表，也可以是包含路径和检查事实的对象。主 chat 仍须核对产物，不因报告的格式正确就认定完成。

报告状态使用 worker 指令中的 progress、ready、blocked、failed、dropped；没有报告时保持 null，不从原生 idle 推断 ready。跟进发送前将原文保存到该工作的 `followups` 列表，每项记录 `request` 与 `delivery`；delivery 为 null 表示尚未发送，调用后记录明确成功、失败或结果不明。发送成功只表示工具接受了消息，不表示后续工作已经完成。结果不明先读 worker 历史核对，不盲目重发。

重启或压缩后重新读所有 linked 会话；文件里的 report 和 cursor 是上次观察，不是当前真相。只报告新增结果或变化的问题，并核对主 chat 最近已交付的内容，避免重复通知。
