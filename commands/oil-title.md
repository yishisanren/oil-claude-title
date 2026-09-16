---
description: 会话自动命名控制：preview（默认）、apply、pause、resume、lock、unlock、status、doctor
argument-hint: "[preview|apply|pause|resume|lock|unlock|status|doctor]"
allowed-tools: Bash(python3 *)
---

按参数「$ARGUMENTS」执行 oil-claude-title 的对应操作。程序入口：

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/oil_claude_title.py"
```

参数对应的子命令：

- 空或 `preview`：`rename`（只预览候选标题，不写入）
- `apply`：`rename --apply`（写入当前会话标题并读回核验）。返回 `renamed` 后，如果当前宿主提供了 `set_session_title` 工具（Claude 桌面版），紧接着用它把同一个标题设给 `self`，让侧边栏缓存与记录一致；没有该工具就跳过。
- `pause` / `resume`：暂停或恢复自动命名
- `lock` / `unlock`：固定或解除固定当前会话标题
- `status`：显示配置和已记录会话数量
- `doctor`：检查运行环境和插件登记状态

当前会话 ID 由程序从 Bash 环境变量 `CLAUDE_CODE_SESSION_ID` 自动读取，不要手动猜测或传入。执行后只汇报返回的状态和实际标题；`preview` 的候选不是已生效的标题。其余规则见插件的 oil-claude-title Skill。
