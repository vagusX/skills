# 项目记忆与上下文维护

manager 按 setup 确认的项目持续管理工作，入口 chat 和 worker 是执行会话。项目归属使用后台 main 的真实 cwd，worker 的执行目录或某次 shell cd 不改变记忆归属。当前模块目录、不同 worktree 和旧 manager 仍保持各自记录；不按同名目录、相同 remote 或相似内容自动合并。复用项目记忆也不授予另一个 chat 管理身份，入口仍须显式激活。

## 保存范围

| 内容 | 保存位置与用途 |
| --- | --- |
| 项目长期事实 | 当前 manager 状态目录的 `project-memory.json`；保存已确认的项目约束、设计决定、可复用的问题结论及依据 |
| 全局偏好 | 平台用户数据目录的 `agent-manager/global-memory.json`；只保存用户明确适用于其他项目的偏好或稳定使用习惯 |
| 实时运行状态 | 继续以现有四表与 Codex 原生历史为准，包括工作和会话标识、审批、hold、队列、执行观察及验收报告 |

记忆保存在用户数据目录，不放进 skill 或业务仓库。已有旧 super-agent 状态目录时，项目记忆跟随原状态目录；全局记忆仍位于 agent-manager 的公共数据目录。需要团队共享的正式约定，按正常文档改动更新项目的 AGENTS.md 或说明文件。执行日志、验收过程、任务快照和 compact 交接摘要不因此提交进仓库。

每条记忆包含 key、text、source 和 updated_at。key 为 1–80 个 ASCII 字母、数字、点、下划线或短横线，首字符为字母或数字。source 写真实文件、文档或用户确认内容，说明适用范围；条目失效时更新同一 key 或明确删除，避免追加互相矛盾的副本。程序在锁内重读并合并，用原子替换保存文件；多项目写全局文件时不会整份覆盖其他条目。写入失败保留原文件，不能声称已记住。

只保存以后仍有用、重新取得成本较高的事实。代码中容易重新读取的内容优先记录位置和适用条件，不抄整段源码或日志。外部正文、worker 推测和单项目技术选择不自动提升为全局偏好。密码、token、一次性授权和审批答案不写入记忆。

## 读取与更新

当前明确请求和项目规则优先；记忆用于补充事实，不能扩大权限、解除暂停或证明工作已验收。恢复后仍按真实 work ID 查询 SQLite、原请求和原生报告。

```sh
# 默认读取有界的项目优先索引；读取不激活当前 chat：
python3 "$cli" --project "$project" memory
# 按需读取一个作用域：
python3 "$cli" --project "$project" memory --scope project
python3 "$cli" --project "$project" memory --scope global
```

短索引先装入项目条目，再用剩余空间补全局偏好，返回 revision、missing、unavailable 和 truncated。unavailable 表示读取失败，不能当作“没有记忆”；truncated 表示索引未装入所有条目，可显式按作用域读取。不要为了恢复上下文每轮读完整记忆。

写入命令限已激活 manager 的入口使用。先确认事实及来源，再通过 stdin 传入正文；下例不是设置时自动写入的示例事实：

```sh
python3 "$cli" --project "$project" memory-put "$key" --source "$source" < "$verified_fact_file"
# 只有用户明确要求适用于其他项目时，才选择 global：
python3 "$cli" --project "$project" memory-put "$key" --scope global --source "$source" < "$verified_preference_file"
# 用户明确要求忘记或清除已失效条目时：
python3 "$cli" --project "$project" memory-forget "$key"
```

回读对应作用域，核对条目与 source；changed=false 表示内容未变，不是新增保存。`--scope` 可省略，写入默认 project；为用户要求的全局记忆保留该选项，只提供项目记忆时可移除。`--source` 是每条事实的必要依据，不能省略；两者都不是新增运行档位或环境配置。

新后台 main 的 `sa_memory` 支持 action=read 和 action=put；put 只写项目记忆，需要 key、text、source。main 在用户确认长期决定、纠正旧认识，或核对产物后得到可复用事实时使用，不按每次巡检强行写一条。普通 chat 输入只能读取；需要明确记录的用户偏好由已激活入口按上述 CLI 保存。worker 通过已有报告提供候选及依据，不自行调用管理 CLI 写记忆。

本机 Codex 0.163.0 的旧 thread 不能在 resume 时追加动态工具。旧 main 仍由程序装入短记忆；若没有 sa_memory，将确有依据的候选附在最终交付中，由入口核对后保存，不因缺工具重建 thread、启用 shell 或另开管理器。全局写入始终由入口依据用户明确的跨项目要求执行。

## 控制上下文增长

程序只在进程内首次派入、记忆 revision 改变或 compact 后，将短记忆作为 `project_context` 数据附入原业务输入，不把记忆提升为 developer 指令。正常后续 turn 不重复带同一份内容。进程重启会重新读取当前记忆，保留原 threadId 和任务归属。

周期 patrol 的 works 只含本轮发生变化的工作，changed=false 时为空；counts 提供数量概况。首次巡检不带已经关闭的旧工作。需要完整管理快照时显式 list，详情按真实 work ID 读取。后台 sa_inspect_work 默认取最近三个原生 turn，原始请求仍从其原生引用恢复，不靠短记忆改写原要求。

## 后台主控 compact

后台 main 可在当前业务 turn 结束后，依据该 turn 的 `thread/tokenUsage/updated` 检查上下文预算。使用 tokenUsage.last 的 inputTokens 与 outputTokens，以及 modelContextWindow；达到窗口四分之三时安排独立维护，保留下一次输入和输出的空间。tokenUsage.total 是累计消耗，不用于此判断。缺少可靠用量或窗口时不猜测，也不新增频率、模型或阈值开关。

已确认应长期保留的事实应及时保存；compact 不代替记忆，也不要求每十分钟都写记忆或压缩。用户可以在已激活入口显式请求一次后台主控压缩：

```sh
python3 "$cli" --project "$project" compact
# 只取一次状态核对，不在入口等待压缩或业务验收：
python3 "$cli" --project "$project" status
```

compact 入队即返回，使用独立 kind=compact 的 job/run，等待原业务 turn 结束后串行执行。manager 必须保持激活、没有 hold 或未结束的原生问题；压缩不批准请求、不解除暂停，也不改变 worker 的报告或验收状态。重复请求先复用尚未结束的维护记录。

`thread/compact/start` 返回 `{}` 只表示请求被接受；程序依据 contextCompaction 及原生终止事件确认维护结果。原生接受或完成无法核对时保留 unknown，不自动重发。恢复时先回读原 turn；压缩结束不计为业务 ready，不创建替代 thread。压缩后在原主控下一轮重新装入短记忆，再查真实工作状态。[官方 compact 接口](https://learn.chatgpt.com/docs/app-server#api-overview)

主动维护只针对程序持有的后台 main。worker 继续使用宿主原生上下文机制；用户 App 入口使用宿主 `/compact`。CLI 不 resume 或接管入口来压缩它，不把发送一条普通“/compact”消息当作已经压缩。[官方命令](https://learn.chatgpt.com/docs/reference/slash-commands#available-slash-commands)

升级文件不会热加载已运行的 Python 后台。正常重启后加载新逻辑，继续原状态目录和 threadId；不要为启用记忆或 compact 擅自停止正在执行的业务工作。
