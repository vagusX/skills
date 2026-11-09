# CLI 运行说明

命令入口是本 skill 的 `scripts/agent_manager.py`。下例的 `$cli` 与 `$project` 表示已经核对的绝对路径，不是新增环境配置；正文通过 stdin 传入。需要 Python 3.9+、PATH 中已登录的 Codex CLI；GitHub 来源另需已登录的 gh。当前使用 Unix 进程锁，支持 macOS/Linux；Windows 未实现。

```sh
python3 "$cli" --project "$project" setup
python3 "$cli" --project "$project" start
python3 "$cli" --project "$project" submit < "$request_file"
# 从 submit 的真实返回中取得 job_id，只确认启动，不等执行或验收：
python3 "$cli" --project "$project" wait-start "$job_id"
# 保存回执后返回入口 chat。后台 run 保持运行。
```

`setup` 只初始化项目绑定。`start` 以独立进程启动同一个 CLI 的 `run`，日志写入数据目录；重复 start 复用已确认的后台进程。`run` 也可在终端前台执行。`stop` 请求后台退出并回查；不把“已请求”冒充已停止。本版本不安装开机启动服务。

普通聊天使用 `chat`；它仍在同一个受管理的主 Codex thread 中保留上下文，但不能调用派工或外发工具。`submit` 用于人明确要求管理的独立工作；`followup <工作 ID>` 继续原 worker。不要仅为读进展调用 followup。

## 默认返回与显式等待

入口 chat 默认在派发并确认启动后立即返回。`submit`/`followup` 只持久化输入并返回 job ID；`wait-start <job ID>` 只查询该次派发对应的 worker，不等待执行、验收或修正结束。取得真实 work_id、thread_id、turn_id、cwd 和运行快照后保存回执即可返回；后台 run 不受入口 turn 结束影响。

启动回执的 phase 由本 CLI 定义：queued 表示已入队，dispatching 表示已分配但尚未观测到本次 worker turn，started 表示本次派发已有真实原生 turn，attention 表示需要查看问题或失败，unknown 表示结果不明。started 不是 ready。`--timeout 0` 只取一次快照；默认启动确认最多 30 秒，超时如实保留 queued/dispatching/unknown，不能反复延长或重复 submit。

默认返回示例：“工作 <真实 work_id> 已启动，thread=<真实 thread_id>。后台继续执行和验收。”若只入队，应写“请求 <真实 job_id> 已入队，尚未确认 worker 启动”，不能把回执未出现当作派工失败而重建。

恢复或用户回来查询时，先复用原数据目录，用 status 或 `wait-start <原 job ID> --timeout 0` 取得当前快照。需要最终内容再 read 原工作/主 thread。查询不会创建 worker；followup 的启动确认不会把原 worker 上一轮当成本轮已启动。原工作仍运行时，修正先入队，不并发恢复同一 thread。

已有 worker 正忙时，followup 入队后立即返回；wait-start 也会直接给出 queued、waiting_for_previous_turn 及可用的 active_turn_id，不消耗启动确认预算等待旧轮结束。这里 turn_id 仍为空，不能拿 active_turn_id 冒充本次跟进已开始。

只有用户明确说“等到完成”“在这里等结果”时，入口才持续有界等待。此时可调用下列命令并结合 status/read 核对真实结果，而不是把它用于默认派工流程：

```sh
python3 "$cli" --project "$project" wait "$delivery_job_id" --timeout 60
python3 "$cli" --project "$project" status
python3 "$cli" --project "$project" read "$work_id"
```

wait 只等指定输入的原生轮次结束，不等所有后台工作或后续修正完成；以原始验收要求和 worker 证据判断。发现耗时补验或修正时仍交原 worker。普通查询、派发或“继续”不继承上一轮的同步等待方式。read 是只读原生历史查询，不恢复、不另开模型 turn。

普通进展留在后台，不向用户逐次报告轮询或无变化的快照；仅最终结果、影响交付的失败、具体人类问题或用户主动查询需要回复。后台主 thread 可进行简短证据核对，但耗时执行、等待和验收由 worker 完成。

`pause <工作 ID> [原停止要求]` 保存 hold 并请求原生中断；`continue <工作 ID> [跟进原文]` 由人明确解除。status 中的 questions 是原生审批或提问，`answer <问题 ID>` 从 stdin 接收人选择对应的原生响应 JSON；旧连接已失效时不会套用旧回答。原生审批的 availableDecisions 和问题格式以实际请求为准。

worker 的业务选择问题在其报告和后台主 thread 回复中，不应误当成原生 questions 列表。发现问题后转述真实问题并返回，不猜答案，也不在入口等人回答。人的业务回答经 `followup <原工作 ID>` 入队；原生审批用 answer。两者都复用既有状态，不创建替代 worker，不自动批准权限。

## 四表与恢复

SQLite 只有 chats、runs、jobs、sources。Codex 保存对话、报告和工具历史；数据库保存映射、hold、运行观察、尚待处理的内容和最小业务提交凭据。确认原生轮次结束后，已投递输入正文从 jobs 清除，保留原生 thread/turn 引用；角色 PE 从参考文件读取，不另建 PE 版本库。

macOS 默认状态目录为 `~/Library/Application Support/agent-manager/<项目路径摘要>/`，其他 Unix 系统使用 `~/.local/state/agent-manager/<项目路径摘要>/`。worker cwd 不改变管理目录。不要把它放进 skill、Git 仓库或网络文件系统。

本 skill 原名 super-agent。对应项目的旧 super-agent 数据目录内已有 state.sqlite3 时，优先原地使用，不移动数据库、不改 threadId；显式传入的 --state-dir 也继续有效。旧版 JSON 的 .super-agent 路径、原生历史中的 SUPER_AGENT_INPUT 标记和 issue 回复标记保留原格式，用于识别已有记录，不是旧技能入口。

一个数据目录只有一个 run 进程持有 OS 锁。每个 chat 的 prepared/running/unknown 运行互斥；有写入目录重叠的 worker 串行。同目录重启继续原 threadId。旧 Codex 进程身份仍匹配时先等待，不因超时、App notLoaded 或日志 EOF 就并发 resume。

中断的原生轮次在确认旧进程结束并核对原生状态后，以恢复提示继续原授权工作；worker 先检查实际文件与外部状态。创建结果不明、输入无法核对或旧 thread 仍 active 时保留 unknown，不重建。旧版 JSON 管理记录不会自动导入或接管。

主线程醒来指受后台管理的原生 Codex 主 thread 开始下一轮；当前 App 入口 chat 不会因此自动插入消息。App 完全退出、系统重启后的自动启动未由本版本配置；再次 start 后会读取持久记录恢复。

所以“入口 chat 返回”与“后台 run 停止”不同：run 仍运行时收取报告并管理后续工作；run 已停止时保留状态供下次 start 核对恢复。不能承诺当前 App 入口结束后自动得到消息，也不为了弥补这一限制擅自创建定时任务。

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
| --project | 可以，默认当前 cwd；需要模块 skills 时应明确对应目录 |
| --state-dir | 可以，默认按项目推导；仅独立测试或明确隔离实例时指定 |
| --github / --command-json / --push | 添加来源时必须选一个，分别表示已有的三种接入方式 |
| --issue | 可以；提供时收取范围限定为一个 issue |
| --allow-reply | 可以且默认关闭；开启表示人明确允许向该 GitHub 来源回复 |
| wait 的 --timeout | 可以，默认 60 秒；仅限制当前等待，不停止后台工作 |
| wait-start 的 --timeout | 可以，默认 30 秒；只限制启动确认，0 为一次快照，不增加执行等待配置 |

模型、profile、轮询周期和并发档位不新增用户配置。原生动态工具是实验接口；执行器沿用当前 Codex 登录与权限配置，不自动批准原生权限，也不修改全局配置来绕过缺失能力。
