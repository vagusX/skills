# Codex CLI 会话操作

此文件保留旧版手工 exec/resume 管理方式。当前四表后台调度使用 [CLI 运行说明](runtime.md) 中的 app-server 入口；不要把本文件用于自动切换或重建后台已经管理的工作。

支持原生 --cd 时，这是新工作的优先入口。本机 2026-11-09 用 PATH 中的 codex-cli 0.163.0 实测了两个模块并行启动、局部 skills 调用、按会话 ID 续跑和持久历史回读。执行前重新读取本机 help 与版本，使用已有登录态和默认模型，不改用户配置。

只在目标执行环境有可用 CLI、exec 支持 --cd/--json/resume 且目标目录可访问时启动。启动前不满足这些条件，可以回退到 App 原生工具及其已保存项目；不得把本机路径当作远程宿主路径。已经启动后出现的任务失败、超时或观察丢失，应在本入口核对，不触发另建 App 会话。

## 启动与项目 skills

使用绝对项目或模块路径设置 --cd。项目 skills 从会话 cwd 向上到仓库根发现；模块中的 `.agents/skills` 需要对应的模块 cwd。先确认目录与交接范围，再启动。不要靠工作中的 shell cd 补启动上下文。

以下是实测的命令形态；变量代表已经核对并正确引用的路径，不是新的配置项。prompt 从文件经 stdin 传入，不把人的原文拼入 shell 命令。

```sh
codex exec --cd "$work_directory" --json --sandbox workspace-write \
  --output-last-message "$final_path" - \
  < "$prompt_path" > "$events_path" 2> "$stderr_path"
```

| 参数 | 能否省略 |
| --- | --- |
| --cd | 本流程需要明确目录，不能省略后假定路径正确 |
| --json | 本流程用于提取真实会话 ID 和执行事件，应保留 |
| --sandbox workspace-write | 可在已有权限配置满足工作时省略；示例是本次测试所用策略，不覆盖人的既有权限选择 |
| --output-last-message | 可以省略，最终回复也能从 JSONL 中读取 |

不传模型或 profile 覆盖，除非人已指定；不使用 --ephemeral，因为本流程要按原 ID 续跑。创建前保存派工意图，启动后立即保留宿主返回的命令句柄、实际 PID（若已取得）和日志位置。

上述命令应交给宿主实际支持的后台进程能力运行；默认只读取 thread.started 和必要的启动观察，保存真实标识后返回入口 chat，不等待前台命令退出。没有已核实的后台托管能力时说明限制，不假装宿主退出后命令仍会继续。

把 worker 指令、原始请求和已有约束放入 prompt。交接要包含 worker 的报告状态约定，不能只列字段名；原生 turn.completed 与 worker 的 ready 是不同事实。不符合约定的报告保留原文，不能靠改写状态字符串宣布验证成功。需要特定项目 skill 时按已发现的真实名称调用，不能猜测名称；测试中按名称调用同时读取了技能正文和其引用文件。

## 等待与结果

CLI 命令可由宿主命令工具持续执行。命令工具返回的 session handle、OS PID 和 Codex 会话 ID 是三种标识，不能互换。

- 从 JSONL 的 thread.started 事件提取 thread_id，写入工作记录的 threadId；在此之前不能猜 ID。
- 默认用实际命令句柄或进程、JSONL 中的真实 ID 作一次启动确认并返回。只有用户明确要求“等到完成”时，入口才持续有界等待执行结束和读取日志；后台管理程序自身可以继续观察。保留标准错误以诊断问题。
- turn.started 仅表示开始。turn.completed 和进程退出码用于判断原生执行情况；错误事件、命令项失败和最终报告仍要检查。
- item.completed 中 type=agent_message 的 text 可能是进展，也可能是最终报告；完整读取最终消息或 output-last-message 文件，再核对实际产物。
- 每轮保存独立日志，避免后续执行覆盖第一轮证据。日志 EOF 或一次读取超时不能证明进程已结束。

只有原执行已确认退出后，才能对同一 ID 启动下一次 resume。观察句柄丢失时先查已有进程与持久日志；无法排除仍在执行就不并发 resume。不同目录且互不影响的工作可以由不同 CLI 进程并行推进。

这是同会话的运行互斥，不是要求入口 chat 留在当前 turn 等待。入口返回不停止进程，也不降低 worker 的验收要求；未退出的跟进保存待发送状态，不能另起同工作的 worker。普通进展不刷屏，最终结果、影响交付的失败或具体人类问题需要报告。单次 codex exec 没有持续管理程序时，不承诺自动开始下一轮或唤醒入口。已激活入口的周期计划由 setup-agent-manager 通过 App 工具管理，见 [周期巡检](runtime.md#周期巡检与进展续跑)；CLI 没有该工具时不自行安装调度器。

## 续跑

先读取已保存的工作记录、人的 hold 和原执行结果，再用真实 ID 续跑：

```sh
codex exec --cd "$work_directory" --sandbox workspace-write \
  resume "$thread_id" --json --output-last-message "$final_path" - \
  < "$followup_path" > "$events_path" 2> "$stderr_path"
```

多工作管理不能用 --last 选择目标。原目录、原会话 ID 与新一轮返回的 thread_id 应一致；目录按同一宿主解析符号链接后的真实路径比较。不一致时先核实，不把新会话冒充原工作。--cd 位于 exec 与 resume 之间；实际参数解析以当前版本 help 为准。

后续请求只带新原文和必要上下文，不重写第一轮请求。恢复同一会话能继续使用已有历史，仍需核对实际文件与外部操作，避免重复执行。

恢复查询只取当前快照；需要修正或回答时继续原会话。需要人的选择时转述问题后返回，人的回答作为下一条原工作输入；不在入口等人、猜答案或自行批准权限。

## 执行记录

工作记录中的 threadId 来自 thread.started.thread_id；directory 记录实际 cwd。没有 App 原生返回值时，hostId、waitCursor 保持 null。CLI 工作增加自定义 executions 列表，每轮追加以下对象，最后一项是最近执行；字段由本文件定义，不是 Codex 官方会话 schema：

```json
{
  "kind": "codex-cli",
  "processHandle": null,
  "pid": null,
  "eventsPath": "<本轮 JSONL 的绝对路径>",
  "stderrPath": "<本轮标准错误的绝对路径>",
  "exitCode": null
}
```

processHandle 只填宿主针对该进程实际返回的句柄，pid 只填实际取得的 PID，拿不到就保留 null。一个宿主句柄若管理多个子进程，不能冒充某个子进程的独立句柄。保留之前的执行记录与日志；exitCode 只在对应进程退出后填写。观察失败与已确认退出分开记录。

本机实测 App read_thread 能读到这些 CLI ID 的持久历史，但返回的 notLoaded 不能证明对应 CLI 进程已停止；App 进程内状态不能替代 CLI 进程和日志观察。不能据此宣称 CLI 运行时的 App 实时通知、侧栏状态或取消能力已验证。

停止只操作自己确实拥有且已核实的命令句柄或进程，使用宿主提供的中断能力并回查。发送信号或丢失句柄都不能保证外部副作用已停止。

来源：[Codex exec 与 resume](https://learn.chatgpt.com/docs/developer-commands#codex-exec)、[Skills 发现范围](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。本文件的旧版手工方式本身不提供 SQLite 调度或入口自动收取；当前四表后台见运行说明。两者均不依赖 DSH。
