# oil-claude-title

[![跨平台验证](https://github.com/yishisanren/oil-claude-title/actions/workflows/test.yml/badge.svg)](https://github.com/yishisanren/oil-claude-title/actions/workflows/test.yml)

让 Claude Code 的会话标题跟上你正在做的事情。每轮对话结束后，后台自动参考最近 3～5 轮内容更新标题；会话再多，也更容易在 `/resume` 列表和侧边栏里找回。

本项目移植自 [oil-oil/oil-codex-title](https://github.com/oil-oil/oil-codex-title)（MIT），命名规则、标题结构与保护逻辑保持一致，宿主接口换成了 Claude Code 的 Stop Hook、会话记录和 headless 模式。

## 一眼看出正在做什么

| 原来的标题 | 更容易找回的标题 |
| --- | --- |
| 回应中文问候 | 🧩 邮箱验证码｜过期排查 |
| 确认注册功能 | 🧩 支付回调｜重复发货修复 |
| 讨论视频标题 | 🎬 图像模型评测｜视频策划 |
| 继续修改 | 🎨 登录表单｜布局优化 |

统一采用 **「类别 emoji + 对象｜目标」**，先找对象，再看正在做什么。

- **对象在前**：把“登录表单”“支付回调”等辨识词放到前面，默认省略外层已有的项目名。
- **类别固定**：🎬 内容制作、🧩 工具开发、🔎 对比调研、🎨 页面设计、📝 方法整理、📅 日程安排、⚙️ 环境配置、💬 一般讨论。
- **名称稳定**：准确的对象名称尽量不变；只在工作目标实质变化时更新；“继续”“推送”不会取代主线。
- **不打断对话**：独立的 haiku 进程在后台命名，不向原对话注入任何消息。
- **跟随你的语言**：根据最近几轮用户消息的主要语言命名，保留产品名；偶尔一句外语不会让标题来回切换。
- **由你控制**：可以预览新标题、固定喜欢的名称，也可以随时暂停或恢复自动命名；用 `/rename` 手动改过的标题会被自动保护。

## 安装

在任意终端执行两条命令：

```bash
claude plugin marketplace add yishisanren/oil-claude-title
```

```bash
claude plugin install oil-claude-title@oil-claude-title
```

克隆到本地后也可以把第一条命令里的仓库名换成本地目录路径（in-place 加载，改源码后新会话即生效）。安装完成后**新开的**会话才会加载 Hook；在新会话里执行 `/oil-title doctor`，`hook.status` 为 `ready` 即表示已登记启用。

也可以直接把下面这段话发给 Claude Code：

```text
帮我安装 GitHub 上的 Claude Code 插件 yishisanren/oil-claude-title：用 claude plugin marketplace add 登记，再用 claude plugin install 安装到 user 范围，然后运行 /oil-title doctor 检查是否生效。
```

默认使用当前登录账号的 **haiku**（关闭扩展思考），每次命名约 6～10 秒、约 1 美分等值额度。

## 日常怎么用

装好后在新会话里正常聊天即可。需要干预时：

- `/oil-title` 预览当前会话的新标题（不写入）
- `/oil-title apply` 立即写入
- `/oil-title pause` / `/oil-title resume` 暂停或恢复自动命名
- `/oil-title lock` / `/oil-title unlock` 固定或解除固定当前标题
- `/oil-title status` / `/oil-title doctor` 查看配置与运行环境

也可以直接说“预览这个会话的新标题”“固定这个会话的标题”“暂停自动命名”，插件自带的 Skill 会调用同一套程序。

## 现状与边界

- macOS + Claude Code 2.1.266 实测：Hook 真实触发、后台命名、标题写入并被后续轮次沿用。Linux 与 Windows 只有 GitHub Actions 上的单元测试覆盖（macOS 3.9/3.13、Ubuntu、Windows 均通过），未在真实 Claude Code 里实测。
- 桌面版（Claude Desktop）侧边栏按自己的缓存显示，并会在下一轮提问时把缓存标题写回记录，所以后台自动改名在桌面会话里不会体现到侧边栏；在会话里执行 `/oil-title apply` 会同时写入记录并同步桌面缓存。`/resume` 列表按会话记录读取，会显示新标题。
- `claude -p` 一次性会话在打印结果后立即退出，异步 Hook 来不及完成，不会被命名；交互式会话不受影响。
- 云端会话、远程存储后端不支持。原版的闲置话题归档功能没有移植：Claude Code 没有对应的归档接口。

[详细使用与数据说明](docs/使用与边界.md) · [命名与回归规范](docs/命名与回归规范.md) · [MIT 许可证](LICENSE)
