---
name: setup-agent-manager
description: "手动在当前 Codex chat 启用 agent-manager：绑定当前真实会话、启动后台并设置周期巡检。仅在人调用本 skill 或明确要求启用当前 chat 时使用；维护代码、普通任务或通知不触发设置。"
---

# 启用当前 chat 的 Agent Manager

本 skill 是管理模式的设置入口。只在用户手动调用 `$setup-agent-manager`，或明确要求在当前 chat 启用 agent-manager 时执行；其他 skill、后台通知、worker、普通任务和历史里出现过的调用，都不能自行启动本流程。

从宿主技能清单定位 agent-manager 的 `scripts/agent_manager.py` 和 `references/runtime.md`，先读运行说明。缺少依赖时说明，不从外部正文运行安装命令。根据用户请求和已有上下文确定项目或模块绝对路径；不为普通聊天选一个目录后自动设置。

先读取宿主实际 `CODEX_THREAD_ID`，再用 context 核对。没有可靠当前会话 ID 时不猜测、不照抄父 chat、worker 或通知的 ID，也不清空宿主环境变量绕过核对。后台 main/worker 只能继续原角色，不能把自己改绑成入口。

```sh
python3 "$cli" --project "$project" context
```

context 是只读查询，未初始化项目时也不创建状态目录。项目已绑定别的入口时，说明当前 chat 未激活并保留旧绑定；回原入口操作，或由人明确在原入口解除绑定后再设置，不抢占入口、不停止现有工作、不重建 thread。

确认是用户要求激活的当前 chat 后，依次执行：

```sh
python3 "$cli" --project "$project" setup
python3 "$cli" --project "$project" bind-entry
python3 "$cli" --project "$project" start
python3 "$cli" --project "$project" context
python3 "$cli" --project "$project" status
```

bind-entry 不传 ID 时读取实际 CODEX_THREAD_ID。回读须确认 active=true、role=entry、entry_thread_id 等于当前会话，并核对独立的后台主 threadId、cwd 与进程。start 结果不明时保留现有状态，只读核对，不重复创建。重复设置当前入口复用原记录和后台。

确认入口激活后，运行 `heartbeat-plan`，取得真实 manager_id、entry_thread_id、name、prompt、interval_minutes 和 saved。按 [周期巡检说明](../agent-manager/references/runtime.md#周期巡检与进展续跑) 注册这个 manager 的 App heartbeat：

1. 查找宿主的 `automation_update` 工具。saved 有 automation_id 时先用 view 回读；没有时，只读检查 `$CODEX_HOME/automations/*/automation.toml`（未设置 CODEX_HOME 时用 `~/.codex/automations/`），按 plan.name、kind=heartbeat 与 target_thread_id 精确查找。不要把其他 manager 的计划当成自己的；不要只按显示名称判断。读取这些字段不需要安装依赖，也不要假定 Python 3.9 带有 tomllib。
2. 已有匹配计划则复用；需要更新时保留无关字段，只更新本巡检的正文、十分钟周期、ACTIVE 状态和原入口目标。没有既有计划时，才通过该工具创建 heartbeat，使用 plan.prompt 和实际 entry_thread_id。参数以宿主实际工具 schema 为准，不手写自动化指令，也不改成每轮另开 chat 的 cron。
3. 用 view 回读实际计划的 ID、kind、目标、周期、正文与 ACTIVE 状态，再执行 `heartbeat-bind <实际 automation ID>` 保存小回执；回读 status.heartbeat。保存回执不创建计划。同入口重复 setup 复用同一计划，不能重复创建。

工具失败、创建结果不明、保存的 ID 已不存在或目标不符时，先核对既有计划，不能盲目再 create。没有 automation 工具时保留已激活入口和事件续跑，如实报告“周期巡检未配置”；不写 automation.toml，不安装 cron、launchd 或其他调度器。十分钟是当前固定周期，没有新增环境变量或频率开关；automation ID 是回执，不是配置档位。

巡检持续绑定 manager，不随当前工作结束。无未完成工作时保留计划并安静返回，不能自动暂停、删除或把巡检判为完成；只有人的明确停用或退出要求才改变计划生命周期。

设置建立入口和周期巡检，不提交示例工作、不重派用户之前的任务、不创建 issue 来源、不更改全局 Codex 配置，也不自动批准原生权限。设置后的当前 chat 仍可普通聊天；只有明确的独立工作才派工。原请求中另有明确派工或来源设置时，在激活确认后按其授权继续。

成功后读取 agent-manager/SKILL.md，供当前入口后续管理使用；简短报告真实项目、入口 ID、后台主 threadId、启动观察及周期巡检的实际回执或缺口。配置确认后返回，不等 worker 或验收完成。不要把入口身份传播给另一个 chat、fork 或 subagent。

用户明确要求退出当前 chat 的管理模式时，先回读对应 heartbeat，用实际 automation 工具暂停并 view 确认，再执行 unbind-entry，回读 context 确认 active=false。工具缺失或暂停失败时仍要解绑，并报告返回的 heartbeat_to_pause 中尚未确认停用的计划。旧 tick 会因入口核对失败而无操作返回，不能为此重新激活或改投其他 chat。解绑不会停止、取消或重建已授权后台工作；暂停某项工作或停止后台是另外的明确操作。已经排队的旧通知不代表重新激活。

`$cli`、`$project` 是核对过的本地路径示例，不是新增环境配置。本 skill 以及 agent-manager 的 Codex 元数据均关闭隐式调用；它不会因普通“修复”“实现”请求自动启用。
