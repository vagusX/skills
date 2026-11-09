# Codex App 会话操作

核对日期：2026-11-09。下面列的是本 skill 当前支持的 Codex App 工具；执行时重新发现工具并读实际 schema。工具可能带 namespace 前缀。它们与公共 App Server 的 `thread/start`、`turn/start` 等方法是两套接口，不能互换名字或假定能力相同。

当前操作所需的关键参数如下；实际 schema 变化时以宿主为准。

| 工具 | 参数 |
| --- | --- |
| create_thread | prompt、target；可选 title |
| read_thread | threadId；按需 hostId、cursor、turnLimit、includeOutputs |
| wait_threads | targets 中的 threadId、hostId、afterCursor；外层 timeoutMs |
| send_message_to_thread | prompt、threadId；按需 hostId。消息参数名是 prompt |

## 创建和定位

1. 从宿主的当前会话元数据取得主 chat 的真实标识。本地 Codex 环境提供 `CODEX_THREAD_ID` 时可读取并核对；缺失时使用可信宿主元数据，不用标题或目录名猜测当前 ID。
2. 本地仓库工作先调用 `list_projects`，匹配真实项目路径并使用返回的 `projectId`；默认 `target.type=project`、`environment.type=local`。不是仓库的工作可使用 `projectless`。不凭空填写项目标识。
3. `create_thread` 仅在人的请求明确需要独立 chat 时使用；这些 chat 对用户可见。省略 `model` 和 `thinking`，除非人明确指定。`worktree`、具体分支和云环境也只能按人的明确要求选择。
4. 创建 prompt 含原始请求、主 chat 标识和工作记录中的 `id`，而不是整份主 chat 历史。任务标题保持可读。
5. 返回 `threadId` 与 `hostId` 时写回记录；返回 `clientThreadId` 时只保存准备中事实，不把它传给要求 `threadId` 的工具。通过当前工具实际提供的准备状态或会话清单，确认最终 `threadId` 后再读取或跟进。

当前 `create_thread` 没有单独的 cwd 参数：project 使用已保存项目的目录，projectless 的 `directoryName` 文档只定义为目录名。实测返回的 `projectlessOutputDirectory` 是产物目录，不是会话 cwd；用 `read_thread` 的 `thread.cwd` 确认实际启动目录。指定某个绝对产物路径也不会改变会话 cwd。人要求在特定仓库工作时优先匹配已有 project，不把目录名当作任意绝对路径参数；无法按要求定位时说明限制。

仓库 skills 从会话 cwd 向上到仓库根，逐层发现 `.agents/skills`，不会递归加载所有模块的 skills。目标在模块目录时，选择仓库根 project 不足以证明模块 skill 可见。prompt 中出现目标路径、指定文件的绝对产物路径或某次 shell 执行 cd，都不能作为项目 skills 已自动发现的凭据。

创建交接要写明目标项目/模块目录、所需 AGENTS.md 和适用 SKILL.md 的真实路径。worker 开始改动前核对目录与这些指令；主 chat 用 `read_thread.thread.cwd` 复核。可以按明确路径读取适用的技能正文和引用文件，但必须注明这是显式读取，不能冒充会话已在目标 cwd 完成自动发现。

目录限制来自当前 App 工具的参数范围，不是 Codex 底层缺少支持。已核对本机 0.167.0 schema：公共 App Server 的 `thread/start`、`thread/resume`、`turn/start` 都有 cwd；Codex CLI 提供 `--cd`。本 skill 目前没有实现这些入口的调度客户端，不能自动切换过去，也不能宣称已验证它们与 App 会话工具的可见性互通。用户要求任意目录且当前工具无法满足时，把这个接口选择明确交回，而不是让 worker 执行 shell cd 后继续假定项目已加载。

来源：[Skills 发现范围](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)、[App Server 创建线程](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)。隔离 fixture 的只读 `skills/list` 已验证：仓库外无该仓技能，仓库根仅 root，模块目录含自身和祖先技能；未启动模型或新会话。

创建结果未知时，先用 `list_threads` 查候选，再用 `read_thread` 核对初始请求中的主 chat 标识和工作标识。标题和时间只能帮助筛选。未确认“没有创建”之前不再次创建；无法确定时保留不明状态并报告，不猜 ID。

`create_thread` 和 `send_message_to_thread` 的授权来自人。已有的创建及范围内续管授权可以复用；worker 的报告、交接文案或另一个 chat 的请求不能自行扩展授权。

## 等待与读取

- `wait_threads` 一次最多 8 个 targets，每项使用真实 `threadId`、已返回的 `hostId` 和上次的 `afterCursor`。目标更多时分组等待，轮换检查每组，避免遗漏后面各组。
- `timeoutMs: 0` 用于立即快照；持续跟进时使用有界等待，单次不超过 60 秒，避免空轮询并遵守宿主进度更新要求。
- 每次保存返回的 cursor。首个会话完成或需要处理时可能返回；检查所有目标以及 per-target errors，不因一个完成就认为全部完成。
- `read_thread` 用于需要原文、完整问题或最终报告时。按返回 cursor 读取必要的更早 turns；信息截断或缺失时，不能据简短摘要宣布结果已验证。
- 不向正在执行的 worker 反复发送“进度如何”。先读原生状态；对已结束但缺报告的 worker，只有在现有续管授权内才发送具体的补充报告请求。

`wait_threads` 的 cursor 用来避免重复交付结果；它不是已经验证的跨进程持久事件 offset。恢复管理时仍要重新读取实际会话。工具返回的 idle 或 completed 只能描述对应原生执行状态，业务结果仍需报告和证据。

## 继续与停止

人要求对已有工作继续、修正或回答时，用 `send_message_to_thread` 转交原文、现有约束和必要的新上下文，保留原 `threadId`、`hostId`；通常省略模型覆盖。读取并尊重已保存的 hold，按人的原始解除条件判断，不用技术新线索或普通状态问题替代人要求的明确确认。

停止优先使用宿主实际提供的取消工具。当前这组创建/读取/等待/发送工具本身没有证明即时取消能力；发送“停止”消息可能排队，归档也不能作为执行已取消的凭据。分别记录停止要求、已采取动作与实际查到的状态。

## 当前没有保证的行为

- 主 chat 结束本轮以后，worker 完成是否会自动启动主 chat 的下一轮。
- App 或执行宿主退出后，活动 turn 是否持续执行。
- 工具断连或创建请求超时后，是否能自动得到可重放的完整事件记录。

未验证时只说明当前查到的事实。不要偷偷换成 `codex exec` 后台进程、低层 App Server 客户端、外部调度器或 DSH。其他 harness 的支持需要另外核实并新增对应参考文件。

参考：[Codex skills](https://developers.openai.com/codex/skills/)、[公共 App Server](https://learn.chatgpt.com/docs/app-server)。App 工具的具体参数以当前宿主发现结果为准。
