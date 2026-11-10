---
name: setup-agent-manager
description: "手动在当前 Codex chat 启用 agent-manager：初始化项目、绑定当前真实会话并启动后台。仅在人调用本 skill 或明确要求启用当前 chat 时使用；维护代码、普通任务或通知不触发设置。"
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

设置只建立管理入口，不提交示例工作、不重派用户之前的任务、不创建 issue 来源、不更改全局 Codex 配置，也不自动批准原生权限。设置后的当前 chat 仍可普通聊天；只有明确的独立工作才派工。原请求中另有明确派工或来源设置时，在激活确认后按其授权继续。

成功后读取 agent-manager/SKILL.md，供当前入口后续管理使用；简短报告真实项目、入口 ID、后台主 threadId 和启动观察。配置确认后返回，不等 worker 或验收完成。不要把入口身份传播给另一个 chat、fork 或 subagent。

用户明确要求退出当前 chat 的管理模式时，执行 unbind-entry 后回读 context，确认 active=false。解绑不会停止、取消或重建已授权后台工作；暂停某项工作或停止后台是另外的明确操作。已经排队的旧通知不代表重新激活，也不能改投另一个入口。

`$cli`、`$project` 是核对过的本地路径示例，不是新增环境配置。本 skill 以及 agent-manager 的 Codex 元数据均关闭隐式调用；它不会因普通“修复”“实现”请求自动启用。
