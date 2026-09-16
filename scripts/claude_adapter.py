"""通过会话记录文件读取/改名，通过临时 headless Claude 进程独立命名。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


class BackendError(RuntimeError):
    pass


class ModelSkipped(BackendError):
    """调用前发现状态已改变；不是模型失败，也不自动重试。"""
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def process_options():
    # 隐藏后台子进程的控制台窗口；不通过 shell 执行模型参数。
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def config_home():
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def native_binary(path):
    """Windows 上要求原生 claude.exe，避免经 cmd.exe 转义 JSON 参数。"""
    if sys.platform == "win32" and Path(path).suffix.lower() in (".cmd", ".bat"):
        raise BackendError("Windows 需要原生 claude.exe（运行 claude install 获取），或通过 configure --claude-bin 指定路径")
    return str(path)


def find_claude(explicit=None):
    if explicit:
        resolved = shutil.which(explicit)
        if resolved:
            return native_binary(resolved)
        raise BackendError("配置的 Claude 可执行文件不存在")
    # 宿主会把自己的可执行文件路径交给 Hook；优先复用，保证版本与登录状态一致。
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
    if execpath and Path(execpath).is_file() and os.access(execpath, os.X_OK):
        return native_binary(execpath)
    for name in (("claude.exe", "claude") if sys.platform == "win32" else ("claude",)):
        path = shutil.which(name)
        if path:
            return native_binary(path)
    for candidate in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude")):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    raise BackendError("未找到 Claude Code；请安装并登录，或通过 configure --claude-bin 指定路径")


def worker_env():
    """去掉当前会话的身份变量，避免临时模型进程把自己当成原会话的一部分。"""
    env = os.environ.copy()
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PROJECT_DIR",
                "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_ENV_FILE", "CLAUDE_PID", "CLAUDE_EFFORT"):
        env.pop(key, None)
    env["OIL_CLAUDE_TITLE_WORKER"] = "1"
    return env


# 这些用户记录是宿主写入的命令回显、通知或中断标记，不是用户请求。
SKIP_PREFIXES = ("<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>",
                 "<local-command-caveat>", "<task-notification>", "<scheduled-task", "<conversation_history>",
                 "[Request interrupted")


def message_text(content):
    """只取纯文本块；工具结果、图片和思考不进入命名上下文。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


class TranscriptBackend:
    """只读解析会话记录；改名时追加与 /rename 相同的元数据行，不改写既有内容。"""
    def __init__(self, projects_dir=None):
        self.projects_dir = Path(projects_dir) if projects_dir else config_home() / "projects"

    def locate(self, session_id, transcript=None):
        if transcript:
            path = Path(transcript)
            if path.is_file():
                return path
            raise BackendError("会话记录文件不存在")
        matches = sorted(self.projects_dir.glob("*/" + session_id + ".jsonl"), key=lambda p: p.stat().st_mtime)
        if not matches:
            raise BackendError("未找到该会话的记录文件；确认会话 ID，或用 --transcript 指定路径")
        return matches[-1]

    def read(self, session_id, transcript=None):
        path = self.locate(session_id, transcript)
        title, cwd, latest, turns, current = "", None, None, [], None
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # 撕裂的尾行或非 JSON 行不参与命名
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "custom-title":
                    # 宿主读取标题时同样取最后一条（last-wins）。
                    if record.get("sessionId") in (None, session_id):
                        title = record.get("customTitle") or ""
                    continue
                if kind not in ("user", "assistant") or record.get("isSidechain"):
                    continue
                if cwd is None and record.get("cwd"):
                    cwd = record["cwd"]
                latest = record.get("uuid") or latest
                message = record.get("message")
                text = message_text(message.get("content")) if isinstance(message, dict) else None
                if kind == "user":
                    if record.get("isMeta") or record.get("isCompactSummary") or not text or not text.strip():
                        continue
                    if text.lstrip().startswith(SKIP_PREFIXES):
                        continue
                    current = {"id": record.get("promptId") or record.get("uuid"), "user": text, "assistant": ""}
                    turns.append(current)
                elif current is not None and text and text.strip():
                    current["assistant"] = text  # 一轮里最后一段助手正文视为最终回答
        return {"name": title, "cwd": cwd, "latest_id": latest, "path": str(path), "turns": [
            {"id": turn["id"], "messages": [{"role": "user", "text": turn["user"]}] + (
                [{"role": "assistant", "text": turn["assistant"]}] if turn["assistant"] else [])}
            for turn in turns]}

    def rename(self, session_id, title, transcript=None):
        path = self.locate(session_id, transcript)
        record = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id},
                            ensure_ascii=False, separators=(",", ":"))
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            torn = False
            if size:
                stream.seek(size - 1)
                torn = stream.read(1) != b"\n"
        payload = (("\n" if torn else "") + record + "\n").encode("utf-8")
        # 与宿主 /rename 的写入方式一致：整行追加、last-wins；不重写历史内容。
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        sidecar = path.parent / session_id / "custom-title.json"
        if sidecar.is_file():
            sidecar.write_text(json.dumps({"customTitle": title}, ensure_ascii=False), encoding="utf-8")


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["keep", "rename"]},
        "title": {"type": "string"}, "reason": {"type": "string"},
    },
    "required": ["action", "title", "reason"],
}


def normalize_project_prefix(candidate, context):
    """去掉与外层目录精确等价的重复前缀，不猜项目别名或修改 keep。"""
    hint = re.sub(r"[\W_]+", "", context.get("project_hint", ""))
    if candidate.get("action") != "rename" or not hint or " " not in candidate.get("title", ""):
        return candidate
    emoji, body = candidate["title"].split(" ", 1)
    # 兼容 kite-lms、Kite LMS、KiteLMS，不把 Maple 错当成 MaplePay。
    pattern = r"^" + r"[\s._-]*".join(re.escape(c) for c in hint) + r"\s+(.+)$"
    match = re.match(pattern, body, re.IGNORECASE)
    if not match:
        return candidate
    remaining = match.group(1).strip()
    if len(remaining) < 2 or re.match(r"^(与|和|到|及|→|->|vs\b|to\b)", remaining, re.IGNORECASE):
        return candidate
    return {**candidate, "title": emoji + " " + remaining}


def _generate_title_once(binary, config, context, plugin_root, *, before_model=None):
    # 只有问候/确认时没有命名证据；确定性保留，避免模型凭空生成“普通讨论”。
    trivial = {"", "你好", "您好", "hi", "hello", "嗨", "谢谢", "好的", "好", "ok", "收到", "继续", "嗯"}
    user_texts = [context.get("original_goal", "")] + [
        message.get("text", "") for turn in context.get("recent_turns", [])
        for message in turn.get("messages", []) if message.get("role") == "user"
    ]
    if all(re.sub(r"[\W_]+", "", text).casefold() in trivial for text in user_texts):
        return {"action": "keep", "title": context.get("current_title", ""),
                "reason": "只有问候或确认，缺少新的命名依据"}, {}
    result, usage = generate_json(binary, config, context, plugin_root / "prompts/naming.md", SCHEMA,
                                 before_model=before_model)
    return normalize_project_prefix(result, context), usage


def generate_json(binary, config, context, policy, output_schema, *, before_model=None):
    """隔离的无工具临时模型：不保存会话，不加载本地定制，不触发 Hook。"""
    deadline = time.monotonic() + config["model_timeout_seconds"]
    # 复用当前登录；--safe-mode 关闭 Hook/插件/CLAUDE.md，--no-session-persistence 不留会话记录。
    with tempfile.TemporaryDirectory(prefix="oil-claude-title-") as tmp:
        args = [
            binary, "-p", "--safe-mode", "--model", config["model"], "--effort", config.get("effort", "low"),
            "--tools", "", "--no-session-persistence", "--strict-mcp-config",
            "--permission-prompts", "none", "--max-budget-usd", str(config.get("max_budget_usd", 0.2)),
            "--output-format", "json", "--json-schema", json.dumps(output_schema),
            "--system-prompt-file", str(policy),
        ]
        env = worker_env()
        if not config.get("thinking"):
            env["MAX_THINKING_TOKENS"] = "0"
        # 配额等待和每次内部重试之后，紧接真实模型进程启动前复核。
        if before_model:
            before_model()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError("状态复核已耗尽本次模型时间预算")
        try:
            proc = subprocess.run(args, input=json.dumps(context, ensure_ascii=False),
                                  capture_output=True, encoding="utf-8", env=env, cwd=tmp,
                                  timeout=remaining, **process_options())
        except subprocess.TimeoutExpired as exc:
            raise BackendError("独立命名模型超时；原标题保留") from exc
        if proc.returncode:
            raise BackendError("独立命名模型失败；请检查登录、模型配置和 doctor")
        try:
            payload = json.loads(proc.stdout)
        except ValueError as exc:
            raise BackendError("独立命名模型输出不是 JSON") from exc
        if not isinstance(payload, dict) or payload.get("is_error"):
            raise BackendError("独立命名模型返回错误；原标题保留")
        if payload.get("permission_denials"):
            raise BackendError("命名模型尝试调用工具，本次结果已丢弃")
        result = payload.get("structured_output")
        if result is None:
            try:
                result = json.loads(payload.get("result") or "")
            except ValueError as exc:
                raise BackendError("独立命名模型未返回结构化结果") from exc
        usage = {key: payload.get("usage", {}).get(key, 0) for key in
                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        usage["total_cost_usd"] = round(float(payload.get("total_cost_usd") or 0), 6)
        return result, usage


def generate_title(binary, config, context, plugin_root, *, before_model=None):
    deadline = time.monotonic() + config.get("model_timeout_seconds", 100)
    candidate, usage = _generate_title_once(binary, config, context, plugin_root, before_model=before_model)
    current = context.get("current_title", "")
    legacy = current.count("｜") != 1 or current.startswith("🛠")
    # 模型偶尔误把旧标题判断为结构合规。只复核一次，不自行猜对象或强制改名。
    # 问候过滤不调用模型且无 usage，仍然直接保留。
    remaining = deadline - time.monotonic()
    if candidate.get("action") == "keep" and legacy and usage and remaining > 0:
        # 格式复核共享首次生成的时间预算，不能使 Hook 的最坏耗时翻倍。
        retry_config = {**config, "model_timeout_seconds": remaining}
        candidate, retry_usage = _generate_title_once(binary, retry_config, {
            **context,
            "naming_feedback": "原标题尚未符合 emoji 对象｜目标结构，或仍使用旧开发图标。请重新核对：有明确对象和目标时只迁移格式，保留准确主线；只有依据不足时 keep。不要误称旧格式已合规。",
        }, plugin_root, before_model=before_model)
        usage = {key: usage.get(key, 0) + retry_usage.get(key, 0)
                 for key in usage.keys() | retry_usage.keys()}
    return candidate, usage
