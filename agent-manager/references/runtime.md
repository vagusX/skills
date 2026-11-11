# CLI 运行说明

命令入口是本 skill 的 `scripts/agent_manager.py`。下例的 `$cli` 与 `$project` 表示已经核对的绝对路径，不是新增环境配置；正文通过 stdin 传入。需要 Python 3.9+、PATH 中已登录的 Codex CLI；GitHub 来源另需已登录的 gh。当前使用 Unix 进程锁，支持 macOS/Linux；Windows 未实现。

## 设置入口与当前会话

用户手动调用 `$setup-agent-manager` 后，由该 skill 初始化项目、绑定当前 chat 并启动后台。agent-manager 只负责激活后的使用；setup-issue-triage 也须先确认已激活，不自动代做入口设置。仅维护这些 skill、日常改代码、同 cwd 已有数据库或复制一段通知，都不进入管理模式。

```sh
python3 "$cli" --project "$project" context
```

context 无额外参数，只读返回 current_thread_id、entry_thread_id、role 和 active；项目尚未初始化时不创建状态目录。只有实际 CODEX_THREAD_ID 与当前入口绑定相同且 role=entry 时 active=true。role=main/worker 是后台管理角色，不能当作入口；other/unbound 表示当前 chat 未激活，terminal 表示没有宿主当前 ID，不据此声称某个 chat 已激活。chat、fork、subagent 和另一个同目录的对话都独立核对身份。

设置 skill 使用以下流程；普通任务不自动执行 setup 或 bind-entry：

```sh
python3 "$cli" --project "$project" setup
# entry_thread_id 来自当前入口 chat，不能使用后台主 threadId：
python3 "$cli" --project "$project" bind-entry "$entry_thread_id"
python3 "$cli" --project "$project" start
python3 "$cli" --project "$project" context
python3 "$cli" --project "$project" memory
python3 "$cli" --project "$project" heartbeat-plan
# active=true 后由设置 skill 注册或复用 App heartbeat，并保存实际回执。
```

设置不派发示例工作。用户另有明确任务时，才继续：

```sh
python3 "$cli" --project "$project" submit < "$request_file"
# 从 submit 的真实返回中取得 job_id，只确认启动，不等执行或验收：
python3 "$cli" --project "$project" wait-start "$job_id"
# 保存回执后返回入口 chat。后台 run 保持运行。
```

`setup` 只初始化项目，不激活会话或派工。`bind-entry` 是设置流程中的显式激活步骤；宿主提供当前 ID 时，目标必须相同，不允许照抄其他 ID 或覆盖不同的现有入口。原后台 main/worker 也不能绑定为入口。同入口重复设置复用记录。

`start` 以独立进程启动同一个 CLI 的 run 和本地面板，日志写入数据目录；重复 start 复用已确认的后台及面板进程。run 也可在终端前台执行。stop 请求后台与面板退出并回查；不把“已请求”冒充已停止。本版本不安装开机启动服务。

来自 Codex chat 的派工、控制、来源写入、答案和 ack 在读取正文、排队或启动子进程前核对激活入口。不匹配时拒绝，不自动绑定、重建或重发。显式的 context/list/status/read 等只读查询不激活会话。人工终端和后台内部操作保留原有能力；启动服务时只在子进程环境移除继承的 CODEX_THREAD_ID，不改父进程环境。该核对是工作流边界，不是 OS 隔离；agent 不能通过清空环境变量规避拒绝。

用户明确退出当前 chat 管理模式时，设置 skill 先暂停并回读对应 heartbeat，再用 unbind-entry 并回读 context，确认 active=false。暂停失败或宿主无该工具时仍解绑，按返回的 heartbeat_to_pause 报告遗留计划；旧 tick 不再操作这个 manager。原后台和已授权工作继续存在，旧通知不代表重新激活。停止某项工作或整个后台仍需分别明确要求。已有绑定不因安装或升级被删除，也不迁走 threadId。

三个 skill 的 Codex 元数据均设置 `policy.allow_implicit_invocation: false`，只允许显式调用；不能省略这项，否则默认恢复自动匹配。设置成功后入口读取 agent-manager 正文用于后续管理，仍按人的请求区分普通聊天与独立工作。[官方调用策略](https://learn.chatgpt.com/docs/build-skills#optional-metadata)

普通聊天使用 `chat`；它仍在同一个受管理的主 Codex thread 中保留上下文，但不能调用派工或外发工具。`submit` 用于人明确要求管理的独立工作；`followup <工作 ID>` 继续原 worker。不要仅为读进展调用 followup。

## 默认返回与显式等待

入口 chat 默认在派发并确认启动后立即返回。`submit`/`followup` 只持久化输入并返回 job ID；`wait-start <job ID>` 只查询该次派发对应的 worker，不等待执行、验收或修正结束。取得真实 work_id、thread_id、turn_id、cwd 和运行快照后保存回执即可返回；后台 run 不受入口 turn 结束影响。

启动回执的 phase 由本 CLI 定义：queued 表示已入队，dispatching 表示已分配但尚未观测到本次 worker turn，started 表示本次派发已有真实原生 turn，attention 表示需要查看问题或失败，unknown 表示结果不明。started 不是 ready。`--timeout 0` 只取一次快照；默认启动确认最多 30 秒，超时如实保留 queued/dispatching/unknown，不能反复延长或重复 submit。

默认返回示例：“工作 <真实 work_id> 已启动，thread=<真实 thread_id>。后台继续执行和验收，请在本入口继续跟进。”若只入队，应写“请求 <真实 job_id> 已入队，尚未确认 worker 启动”，不能把回执未出现当作派工失败而重建。

恢复或用户回来查询时，先复用原数据目录，用 list 或 `wait-start <原 job ID> --timeout 0` 取得当前快照，调度诊断用 status。需要最终内容再 read 原工作/主 thread。查询不会创建 worker；followup 的启动确认不会把原 worker 上一轮当成本轮已启动。原工作仍运行时，修正先入队，不并发恢复同一 thread。

已有 worker 正忙时，followup 入队后立即返回；wait-start 也会直接给出 queued、waiting_for_previous_turn 及可用的 active_turn_id，不消耗启动确认预算等待旧轮结束。这里 turn_id 仍为空，不能拿 active_turn_id 冒充本次跟进已开始。

只有用户明确说“等到完成”“在这里等结果”时，入口才持续有界等待。此时可调用下列命令并结合 status/read 核对真实结果，而不是把它用于默认派工流程：

```sh
python3 "$cli" --project "$project" wait "$delivery_job_id" --timeout 60
python3 "$cli" --project "$project" status
python3 "$cli" --project "$project" read "$work_id"
```

wait 只等指定输入的原生轮次结束，不等所有后台工作或后续修正完成；以原始验收要求和 worker 证据判断。发现耗时补验或修正时仍交原 worker。普通查询、派发或“继续”不继承上一轮的同步等待方式。read 是只读原生历史查询，不恢复、不另开模型 turn。

普通进展留在后台，不向用户逐次报告轮询或无变化的快照。入口报告最终结果、影响交付的失败、具体人类问题和主动查询；已设置周期巡检时，将本轮实际变化合并成简短摘要。后台主 thread 可进行简短证据核对，但耗时执行、等待和验收由 worker 完成。

`pause <工作 ID> [原停止要求]` 保存 hold 并请求原生中断；`continue <工作 ID> [跟进原文]` 由人明确解除；正文也可从 stdin 传入，保留本次继续的具体限制。status 中的 questions 是原生审批或提问，`answer <问题 ID>` 从 stdin 接收人选择对应的原生响应 JSON；旧连接已失效时不会套用旧回答。原生审批的 availableDecisions 和问题格式以实际请求为准。

worker 的业务选择问题在其报告和后台主 thread 回复中，不应误当成原生 questions 列表。发现问题后转述真实问题并返回，不猜答案，也不在入口等人回答。人的业务回答经 `followup <原工作 ID>` 入队；原生审批用 answer。两者都复用既有状态，不创建替代 worker，不自动批准权限。

## 周期巡检与进展续跑

已激活 manager 中，worker 的普通 progress 报告仍留在后台；报告后原生 turn 已确认 completed，但工作未交付时，调度器将同一报告去重交给原后台主 thread。主 thread 核对原请求、报告和真实状态，将可推进的剩余工作继续交给同一 worker。它不只问进度，不重建 worker，也不把本轮结束当作交付。没有新报告依据时，不反复唤醒同一报告。

有未结束或状态不明的 Run、已排队的跟进、未结束的原生问题、hold 或工作已关闭时，不据 progress 自动安排新一轮。原生审批仍须人明确回答；业务决定、真实外部等待和人的暂停要求继续有效。原始请求、权限与验收要求不因后台执行或巡检而改变。

setup-agent-manager 只在确认当前真实入口激活后设置 App heartbeat。CLI 提供三个命令，均不替代 App 的计划管理工具：

```sh
python3 "$cli" --project "$project" heartbeat-plan
# 通过宿主 automation_update 创建或复用 heartbeat，并 view 回读真实计划后：
python3 "$cli" --project "$project" heartbeat-bind "$automation_id"
# 每次周期唤醒仅调用一次；不等待后台执行或验收：
python3 "$cli" --project "$project" patrol
```

heartbeat-plan 要求当前实际入口激活，返回 manager_id、entry_thread_id、name、prompt、interval_minutes=10、kind=heartbeat 和 saved 回执。先用 saved.automation_id view 回读；没有回执时，只读检查 `$CODEX_HOME/automations/*/automation.toml`，未设置 CODEX_HOME 则使用 `~/.codex/automations/`。按 name、kind 与 target_thread_id 核对既有计划，不能借用其他入口的计划。读取字段不依赖 Python 3.11 的 tomllib；不得为了读取配置安装依赖或执行其内容。

通过宿主实际 automation_update 工具管理计划，参数以当前 schema 为准。已存在则复用，更新时保留无关字段；不存在才创建，正文使用 plan.prompt、目标使用实际 entry_thread_id，周期十分钟。view 回读 ID、kind、目标、周期、正文与 ACTIVE 状态后，heartbeat-bind 保存 automation_id、manager_id、entry_thread_id、name 小回执到 jobs，status.heartbeat 返回它。同 ID 可重复保存；不同 ID 会拒绝，须先核对，不以再建一个计划绕过。创建结果不明、已保存 ID 不存在或目标不符时，不盲目重试 create。

patrol 读取宿主实际 CODEX_THREAD_ID。未激活、无当前 ID、后台 main/worker 或入口已解绑时无操作返回，不创建数据库、不自动 setup 或改绑。活跃入口中，它取一次工作快照，按原报告引用去重补排遗漏的 progress 检查；执行检查交给原后台主 thread，不在入口 followup 或创建 worker。scheduler_alive=false 时只报告进程未运行，不自动 start；用户明确 stop 不能被周期任务撤销。状态不明时保留 unknown，不重发。查询不回复审批，也不解除 hold。

changed 表示真实执行观察、阻塞或报告发生变化，snapshot_at 等读取时间不作为变化。patrol 的 works 只返回本轮变化的工作，changed=false 时为 []；首次不带已关闭的旧工作，counts 提供 registered、open 和 pending_native_questions 数量。需要完整列表时显式 list，不因一轮巡检反复读全部历史。无变化不重复汇报；有变化时简短说明事实和下一步。原生问题仍沿 inbox/ack/answer 流程处理，只询问未处理的有效请求，提问后返回。周期唤醒不要求入口持续轮询或等待，后台也不因入口结束而停止。

heartbeat 是持续绑定 manager 的计划。当前没有未完成工作或本次验收已经结束时仍保留计划，空闲返回等待未来工作；不能自行暂停、删除或宣布整个巡检完成。只有人的明确停用巡检或退出管理模式请求才停止计划。

无 automation 工具的 CLI 宿主可以使用事件续跑和显式 patrol，但不能声称已设置周期唤醒；不自动安装 cron、launchd，不直接写 automation.toml。heartbeat 回执不是频率配置，十分钟周期不新增环境变量或开关。App 内定时任务复用原 chat 上下文；本地项目执行依赖电脑开机、App 运行和目录可用，离线补跑不作保证。[官方定时任务说明](https://learn.chatgpt.com/docs/automations#schedule-a-task-inside-a-chat)

升级文件不会热更新已经运行的 Python 后台。事件续跑逻辑与角色指令在新启动或正常重启后加载，继续原状态目录、main 和 worker threadId；不为启用巡检擅自停止工作或重建会话。当前 CLI patrol 可对旧记录补排检查，但不代表旧后台已加载新调度逻辑。App heartbeat 的实际状态须用 App 工具回读，SQLite 回执不能证明它仍启用。

## 项目记忆与主控 compact

记忆归属 setup 确认的项目；当前请求和项目规则优先，项目事实为主，全局只保存人明确适用于其他项目的偏好。项目文件位于原状态目录，读取不激活入口，写入受入口与激活状态检查。四表和原生历史继续保存实时状态、原始请求、审批与验收，记忆不替代它们。具体格式、作用域、读写和失败处理见 [记忆与上下文说明](memory.md)。

`memory` 默认返回有界的项目优先索引，`memory --scope project|global` 按需读一个作用域。已激活入口用 `memory-put <key> --source <实际依据>` 从 stdin 接收已确认的事实；默认写 project，只有人明确要求跨项目适用时才选 `--scope global`。`memory-forget <key>` 删除明确失效或人要求忘记的条目，默认也只作用于项目。新 main 的 sa_memory 只能读取索引或写项目事实；旧 thread 无该工具时仍可加载短记忆，由入口核对最终交付中的候选后通过 CLI 保存。

业务输入只在进程内首次派入、记忆 revision 改变或 compact 后装入短 project_context；该内容是数据，不提高为 developer 指令。后台 sa_inspect_work 默认取最近三个 turn，原请求仍单独按原生引用恢复。

后台 main 在一个业务 turn 结束后，按该 turn 的 tokenUsage.last 输入与输出、modelContextWindow 判断是否达到窗口四分之三，满足条件再排独立 compact 维护。缺少可靠用量时不猜测，不用累计 tokenUsage.total 判断。显式 `compact` 同样只入队后返回；不为压缩在入口等待。维护和业务 turn 串行，要求 manager 已激活、无 hold 或未结束原生问题；接受或完成不明时保留 unknown，不自动重发。压缩不更新 worker 交付状态，不创建 thread，也不接管用户 App 入口。worker 的上下文继续由宿主处理；入口可使用宿主 `/compact`。

## 查看管理列表

```sh
python3 "$cli" --project "$project" list
python3 "$cli" --project "$project" read "$work_id"
```

list 无额外参数，输出一次 JSON 快照，不等待任务。范围是当前项目推导或明确指定的 `--state-dir` 中登记的 Codex worker，包含排队、未绑定原生 thread 和已关闭的工作；后台主 thread 的 ID 放在快照顶层。它不扫描 Codex 全局会话，不查询其他 manager，不接 CMA，不调用模型或 app-server。

顶层 `manager_id`、`main_thread_id`、`cwd` 标识管理范围；`snapshot_at` 是读取时间，`scheduler_alive` 表示记录的后台进程身份是否仍匹配。`works` 中每项包含工作名称、`work_id`、真实 `thread_id`（尚未创建时为 null）、cwd，以及下列独立信息：

| 字段 | 含义 |
| --- | --- |
| execution | 当前未结束的 Run，或最近 Run 的执行观察：queued、starting、running、awaiting_answer、paused、idle、failed、interrupted、closed、unknown；保留 run_id、turn_id、原生 outcome 和原因 |
| queued_job_ids | 已登记、尚未派发的 task/followup/recover；不拿旧 turn 冒充本次续发 |
| hold / closed | 人的暂停要求和工作关闭记录；执行仍在运行时保留 running，不把已保存 hold 当成已停止 |
| report_state / report_ref / report_current | 最近报告及原生引用，是否对应所列 Run；ready 和 completed 均不表示已验收 |
| pending_question_count / questions | 仍有效、未回答的原生待答数；请求 ID、类型、答案阶段、live 与 needs_answer，不复制原题、秘密输入或答案 |

执行中的进程身份无法确认、创建结果不明或原有 unknown 未核对时，显示 unknown，不自行恢复。started_at 是调度记录时间，ended_at 是停止观察时间，均不充当最近进展或心跳时间。列表是本地记录加进程核对的快照，不代替 read 的原生历史或真实验收。

答案已排队、已发送、结果不明或请求失效时，不计入待用户回答数，仍保留相应记录供 `answer-status <question_id>` 核对；已关闭的请求不列入 questions。具体原题由 read/inbox 取得，秘密问题通过用户明确打开的面板处理。业务问题只在 worker 报告与原生历史中，不根据 blocked 推算数量或将它当作原生审批。

后台主 thread 使用只读动态工具 `sa_list_work({})`，与 CLI 共用上述查询；普通聊天也可查询，不会创建 effect job。旧 thread 的工具 schema 在创建时确定，本机 Codex 0.163.0 的 thread/resume 不支持追加 dynamicTools。兼容入口为 `sa_inspect_work({"work_id": "<本轮 envelope.chat_id>"})`，参数必须是当前 manager 的真实工作 ID；它返回同一列表。worker 无权使用这两个管理查询入口。

升级文件不会热更新已经运行的 Python 后台；新启动或正常重启后加载代码和角色指令，继续原数据目录与原 threadId。不要仅为启用列表查询擅自停止正在执行的工作或重建会话。CLI list 不依赖后台重启，可立即读取已有记录。

## Desktop 名称与查找

后台主 thread 的原生名称是 `agent-manager · 主控 · <cwd 项目名>`，worker 是 `agent-manager · <原工作名称>`。创建取得并保存真实 threadId 后、首条任务输入前，通过 thread/name/set 设置；正常恢复旧 thread 时也补齐，重复 bind 不叠加前缀。SQLite 中的原始 title 和任务正文保持原样，入口 chat 不改名。名称只用于识别，不授予管理身份。

命名请求有界等待；失败或超时只记录命名未确认，继续原授权派工，不把已创建的 thread 改成创建不明，不重建会话。后续正常 bind 可重设同一名称。后台旧 Python 进程不会热加载代码，不为改标题停止或重建正在执行的工作。用户明确要求为已有会话补名时，可用 Desktop 的实际改名工具按已核对的原 threadId 操作，不需要 resume 或新建 turn。[官方命名接口](https://learn.chatgpt.com/docs/app-server#api-overview)

CLI 创建的是原生 Codex thread，能否出现在某个 Desktop 项目分组还取决于 App 的收录与筛选。当前程序设置 cwd，但不向 Desktop 登记项目归属；临时目录中的会话可能没有 projectId。应按 agent-manager 前缀或真实 threadId 查询最近会话/历史，不能把没有出现在预期项目下当成创建失败。前缀不保证所有宿主或视图都显示。

本机 schema 的 thread/list 默认筛选交互来源，cwd 筛选是精确匹配。程序的 list 则只汇总自己登记的工作，两者范围不同。不要为了在 UI 找到会话改 source、改 cwd、另建 worker 或迁移正在执行的工作；继续用 list/read 核对原 threadId。[列表筛选](https://learn.chatgpt.com/docs/app-server#list-threads-with-pagination--filters)

## App 占用提示

后台主 thread 和 worker 由 CLI 的 app-server 持有。当前 Codex 对同一 thread 只允许一个进程写入；直接在 App 续聊这些会话可能显示“已在另一个应用中打开”，原生 resume 则返回 already has an active writer。这条提示本身不能证明任务失败。

日常操作继续使用原入口 chat：read 只读历史，followup 向原 worker 入队，原生审批经 status.questions 和 answer 转交。后台持有会话时不让 App 同时 resume，不以创建聊天分叉或停止后台来消除提示。入口 chat 与后台主 thread 的 ID 必须分开保存，不能把入口改绑到后台会话。

绑定入口后，原生请求经队列唤醒入口 chat，由入口向用户提问，再用 answer 经后台原连接回复；也可明确打开本地面板处理。它不接管 worker 的原生审批卡。官方协议要求持有连接的客户端展示请求并回传决定。[审批协议](https://learn.chatgpt.com/docs/app-server#approvals)

App 的 notLoaded 或持久历史中重建的 interrupted 状态，不能单独作为 CLI 已停止的凭据；还要核对原进程身份、运行观察和待审批请求。审批等待时不重复派工，也不把之前对其他命令的批准套用到本次请求。

## 本地审批与提问

面板是读取同一 SQLite 队列的小进程，不加载或接管原生会话。start/run 自动启动它；要给升级前已运行的后台接上面板，或重新打开页面，使用：

```sh
python3 "$cli" --project "$project" panel
```

后台不自动打开网页或抢焦点，start/run、面板重启和新问题都不会打开浏览器。只在用户明确要查看界面时执行 panel；该命令才打开页面，浏览器无法启动时仍返回本地访问地址。

绑定入口后不重复发面板系统通知。未绑定入口时，一批待答请求只尝试发一次系统通知；在它们处理完之前，新问题合并到同一批，不逐项重复通知。空队列、已回答、失效请求和普通进展不通知。系统通知能否显示取决于本机权限和勿扰设置，程序不修改这些设置；失败记录到 panel.log。无图形桌面的宿主需自行打开该宿主的本地页面，不将服务暴露到公网。

面板支持命令审批、文件修改审批、原生 requestUserInput、权限子集选择和 MCP 的常见平面表单/URL 请求。选项、问题 ID、路径对象与回答类型沿用真实请求，不从自然语言猜选项。秘密文本使用密码框；文件差异无法取得或表单格式不支持时，明确显示缺口，不提供未经支持的允许提交。取消或拒绝只在原请求实际支持时发送；原生文本问题关闭页面后继续待答。

回答前重新核对原进程、thread、turn 和请求状态；只向仍有效的原连接排入回答。提交后先显示“等待后台确认”，收到原生 resolved 后才移除。重复点击、已回答或断连失效的请求不会触发新执行或新 worker。面板关闭不暂停工作，页面返回不代表替用户批准。

服务只监听本机 127.0.0.1 的自动端口，使用独立访问凭据，并拒绝跨站提交；端口与凭据由程序生成，不新增用户配置。运行元数据位于原数据目录，仍只有四张数据库表。panel-stop 只关闭面板，不停止 worker；重新启用用 panel 或 start。stop 同时关闭两者。面板本身不调用模型。

此界面处理 app-server 原生审批、提问和工具表单。worker 在 sa_report 中报告的业务问题仍按原流程进入后台主 thread，不把业务回答当作原生权限授予。系统重启后的服务自动启动不在本版本内。秘密问题不要在入口 chat 索取，明确打开面板处理。

## 四表与恢复

SQLite 只有 chats、runs、jobs、sources。Codex 保存对话、报告和工具历史；数据库保存映射、hold、运行观察、尚待处理的内容和最小业务提交凭据。确认原生轮次结束后，已投递输入正文从 jobs 清除，保留原生 thread/turn 引用；角色 PE 从参考文件读取，不另建 PE 版本库。

macOS 默认状态目录为 `~/Library/Application Support/agent-manager/<项目路径摘要>/`，其他 Unix 系统使用 `~/.local/state/agent-manager/<项目路径摘要>/`。worker cwd 不改变管理目录。不要把它放进 skill、Git 仓库或网络文件系统。

本 skill 原名 super-agent。对应项目的旧 super-agent 数据目录内已有 state.sqlite3 时，优先原地使用，不移动数据库、不改 threadId；显式传入的 --state-dir 也继续有效。旧版 JSON 的 .super-agent 路径、原生历史中的 SUPER_AGENT_INPUT 标记和 issue 回复标记保留原格式，用于识别已有记录，不是旧技能入口。

一个数据目录只有一个 run 进程持有 OS 锁。每个 chat 的 prepared/running/unknown 运行互斥；有写入目录重叠的 worker 串行。同目录重启继续原 threadId。旧 Codex 进程身份仍匹配时先等待，不因超时、App notLoaded 或日志 EOF 就并发 resume。

中断的原生轮次在确认旧进程结束并核对原生状态后，以恢复提示继续原授权工作；worker 先检查实际文件与外部状态。创建结果不明、输入无法核对或旧 thread 仍 active 时保留 unknown，不重建。旧版 JSON 管理记录不会自动导入或接管。

“入口 chat 返回”与“后台 run 停止”不同：run 仍运行时收取报告，并通过原生队列通知绑定入口；run 停止时保留状态供下次 start 核对恢复。周期巡检由已激活入口的设置流程通过 App 工具管理，Python 后台不自行安装定时任务或开机启动服务。

## 入口通知与回执

`bind-entry [threadId]` 核对原生 thread 后保存当前入口的激活绑定，不 resume、不切换会话执行进程。ID 可省略，默认使用 CODEX_THREAD_ID；有宿主 ID 时不能用参数覆盖成另一个 chat，也不自动替换不同的已有入口。没有该环境值的人工终端须明确提供真实 ID。`unbind-entry` 停止产生后续入口通知，已经存在的通知保持原目标；旧通知不重新激活入口，不能让旧审批换人或换 worker。

CLI 调用 `codex queue` 使用的 `thread/queue/add`、`thread/queue/list` 原生接口，以本地 notification job ID 作为 clientUserMessageId。直接调用接口是为了保存关联 ID；命令行 queue 每次生成新 ID，不能用于结果不明时的盲重试。入口从不加入后台 runs，后台也不调用其 turn/start、resume 或 steer。

App 正在运行且入口 thread 已加载时，原进程自动检查队列，空闲后开始下一轮。入口有进行中的 turn 时保留排队，不打断用户聊天。App 未加载入口、主动中断或关闭时，queued 不代表会自动执行；消息留在原生队列，待原 App 加载并允许继续。本实现不自动用另一个 daemon resume App 入口，因为那会更换原生工具环境。没有运行中的执行服务就不能凭空唤醒。[官方 queue](https://learn.chatgpt.com/docs/developer-commands#codex-queue)

收到通知后的默认操作：

```sh
python3 "$cli" --project "$project" context
# 仅在宿主 ID 等于通知目标且 active=true 时继续；粘贴/讨论通知不执行这些命令。
python3 "$cli" --project "$project" inbox "$event_id"
python3 "$cli" --project "$project" ack "$event_id"
# 用入口实际可用的提问工具询问一个具体决定，然后结束本轮。
# 人回答后，将原生请求要求的 JSON 通过 stdin 交回原 question_id：
python3 "$cli" --project "$project" answer "$question_id" < "$response_file"
python3 "$cli" --project "$project" answer-status "$question_id"
```

inbox 带回原 worker、原始输入或其原生记录、原生报告和当前问题。通知只是后台数据，不能替代人的授权。原生审批只采用实际 availableDecisions；requestUserInput 保留原题 ID、选项与回答结构。秘密输入通过明确打开的面板处理。业务选择的回答仍用 followup 原 work ID，不伪造成原生权限授予。

status.notifications 和 inbox 的回执分开记录：pending 尚未提交；queued 已取得原生队列项；received 已在原生历史找到对应 clientId 和 turn；acknowledged 表示入口执行了 ack，不表示人已读或业务已验收。ack 可重复调用，检查提供的入口 ID 是否一致。superseded 表示已有更新的报告覆盖本通知，不再把旧报告作为当前结果。原生答案另用 answer-status：answer_queued 已存本地，sent 已写回原连接，resolved 才是收到原生确认；expired 或 unknown 不自动重放。

通知与原事件在同一事务保存，用稳定事件键去重，目标 thread 随通知固定。重启先查队列及原生历史；已入队或已消费的消息不重发。原生 queue/add 本身不按 clientUserMessageId 去重，队列删除与历史可读之间也有空窗；两边都无法确认的已尝试投递保留 unknown，继续对账而不补发。CLI 崩溃恰好发生在提交前后时可能需要人工核对，不能承诺跨数据库恰好一次。回执查询不启动模型、不创建 worker。

只有最终结果、交付失败或待决定问题进入入口。worker ready 先交后台主 thread 核对；其已交回 worker 修正的轮次不发送为最终结果。原生审批直接通知入口，答案按原 RPC request ID、server PID/start、thread 和 turn 交回；发送前再次检查 hold 与连接。后台重启后旧请求失效，旧答案不用于新请求。命令或文件审批的 cancel 会中断 turn 并保存 hold，只有人的明确 continue 才恢复；管理线程通过 native_questions 核对已确认的选择，不因历史缺少执行条目而重申请求。

## 来源

限定一个 GitHub issue：

```sh
python3 "$cli" --project "$project" source-add --github owner/repo --issue 123
```

显式指定 issue 时首次把正文和已有评论作为一次原始输入；后续正文或评论变化继续原工作。省略 issue 时读取绑定仓库在 setup 来源时间之后更新的 issues。GitHub 回复默认不发送；只有人明确授权时加 `--allow-reply`。本地账号也可能是发起任务的人，因此按实际 outbox 回执/标记过滤自己的输出，不按用户名整批过滤。

任意第三方轮询脚本：

```sh
python3 "$cli" --project "$project" source-add --command-json '["python3","/absolute/path/poll.py"]'
```

脚本从 stdin 收到 `{"cursor":...}`，stdout 返回 `{"events":[...],"cursor":...}`。每项必须有稳定且非空的 key、object_key、text：key 区分本次事件，object_key 关联同一项长期工作，text 保留原始请求。来源和 cursor 只有在 jobs 入库同一事务成功后才更新。按 argv 执行，不拼 shell；脚本非零退出或 JSON 无效时保留原游标。

已有 trigger 使用 `source-add --push <来源名>` 绑定，再通过 `ingest <source ID>` 从 stdin 提交相同的事件格式。它仅是本地接入，不提供公网 HTTP 服务。`poll [source ID]` 可显式立即收取，日常后台每约 60 秒收取；`source-disable <source ID>` 停止后续收取，保留记录。已进入工作队列的请求如需停止，另用 pause。

## 参数

| 参数 | 能否省略 |
| --- | --- |
| bind-entry 的 threadId | 可省略，读取 CODEX_THREAD_ID；没有宿主值时必须提供真实 ID |
| ack 的 --thread | 可省略，读取实际 CODEX_THREAD_ID；有宿主 ID 时不能冒认另一个入口 |
| --project | 可以，默认当前 cwd；需要模块 skills 时应明确对应目录 |
| --state-dir | 可以，默认按项目推导；仅独立测试或明确隔离实例时指定 |
| --github / --command-json / --push | 添加来源时必须选一个，分别表示已有的三种接入方式 |
| --issue | 可以；提供时收取范围限定为一个 issue |
| --allow-reply | 可以且默认关闭；开启表示人明确允许向该 GitHub 来源回复 |
| wait 的 --timeout | 可以，默认 60 秒；仅限制当前等待，不停止后台工作 |
| wait-start 的 --timeout | 可以，默认 30 秒；只限制启动确认，0 为一次快照，不增加执行等待配置 |
| heartbeat-bind 的 automation_id | 必须来自 App 工具创建或回读的真实计划；该命令只保存回执 |
| memory / memory-put / memory-forget 的 --scope | 可以省略；memory 默认短项目优先索引，写和删除默认 project。为明确授权的全局记忆保留，只提供项目记忆时可移除 |
| memory-put 的 --source | 必须提供实际依据；是事实来源，不是运行配置 |

模型、profile、轮询周期、compact 阈值和并发档位不新增用户配置。原生动态工具是实验接口；执行器沿用当前 Codex 登录与权限配置，不自动批准原生权限，也不修改全局配置来绕过缺失能力。
