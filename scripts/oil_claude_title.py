#!/usr/bin/env python3
"""会话自动命名入口；Hook 只派生后台 Worker，并始终向宿主返回空 JSON。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from claude_adapter import (BackendError, ModelSkipped, TranscriptBackend, config_home, find_claude,
                            generate_title, process_options)
import file_lock

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "enabled": True,
    "model": "haiku",
    "effort": "low",
    "thinking": False,
    "claude_bin": None,
    "recent_turns": 5,
    "max_context_chars": 14000,
    "model_timeout_seconds": 100,
    "max_parallel_workers": 2,
    "max_budget_usd": 0.2,
}
EMOJI = ("🎬", "🧩", "🔎", "📝", "📅", "🎨", "⚙️", "💬")
POLICY_VERSION = 7
PLUGIN_NAME = "oil-claude-title"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


def data_dir():
    override = os.environ.get("OIL_CLAUDE_TITLE_DATA")
    return Path(override).expanduser() if override else config_home() / PLUGIN_NAME


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON 文件必须是对象")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def load_config(root):
    config = DEFAULTS | read_json(root / "config.json")
    for key in ("enabled", "thinking"):
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    for key, lower, upper in (("recent_turns", 3, 5), ("max_context_chars", 3000, 20000),
                              ("model_timeout_seconds", 10, 110), ("max_parallel_workers", 1, 8)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError(f"{key} 必须在 {lower}～{upper} 之间")
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise ValueError("model 不能为空")
    if config["effort"] not in ("low", "medium", "high"):
        raise ValueError("effort 必须是 low、medium 或 high")
    budget = config["max_budget_usd"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0.01 <= budget <= 5:
        raise ValueError("max_budget_usd 必须在 0.01～5 之间")
    return config


def valid_id(value):
    return str(uuid.UUID(value))


@contextmanager
def thread_lock(root, session_id, wait_seconds=0):
    # 内核锁在进程结束后释放，避免崩溃留下永久锁或两个 Worker 覆盖结果。
    path = root / "locks" / (session_id + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                file_lock.acquire(stream)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        try:
            yield True
        finally:
            file_lock.release(stream)


@contextmanager
def worker_slot(root, limit, wait_seconds):
    """不同会话共享进程池配额，等待时间算入模型预算。"""
    deadline = time.monotonic() + wait_seconds
    while True:
        for index in range(limit):
            with thread_lock(root / "worker-pool", str(index)) as acquired:
                if acquired:
                    yield True
                    return
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def limited_title(binary, root, config, context, *, before_model=None):
    deadline = time.monotonic() + config["model_timeout_seconds"]
    with worker_slot(root, config["max_parallel_workers"], config["model_timeout_seconds"]) as acquired:
        remaining = deadline - time.monotonic()
        if not acquired or remaining <= 0:
            raise BackendError("后台命名并发已满；本次保留原标题")
        return generate_title(binary, {**config, "model_timeout_seconds": remaining}, context, ROOT,
                              before_model=before_model)


def ensure_title_active(session_id, root):
    if not load_config(root)["enabled"]:
        raise ModelSkipped("disabled")
    if read_json(state_path(root, session_id)).get("locked"):
        raise ModelSkipped("locked")


SETTLE_TIMEOUT = 5


def read_settled_thread(backend, session_id, transcript, event_turn, timeout=None):
    """确认 Stop 对应的轮次已写入记录；宿主异步落盘尚未完成时短暂轮询。"""
    deadline = time.monotonic() + (SETTLE_TIMEOUT if timeout is None else timeout)
    while True:
        thread = backend.read(session_id, transcript)
        ids = [turn["id"] for turn in thread.get("turns", [])]
        if event_turn in ids:
            if ids[-1] != event_turn:
                return thread, "outdated_event"
            if any(m["role"] == "assistant" for m in thread["turns"][-1]["messages"]):
                return thread, None
        if time.monotonic() >= deadline:
            # 事件轮次始终没有出现：记录尚未落盘或被过滤，本次不命名；只有用户消息的轮次仍允许命名。
            return thread, None if event_turn in ids else "turn_not_settled"
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def state_path(root, session_id):
    return root / "sessions" / (session_id + ".json")


def audit(root, session_id, result):
    path = root / "logs" / (session_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 256 * 1024:
        os.replace(path, path.with_suffix(".previous.jsonl"))
    # 不保存对话原文、模型提示词、推理或 CLI 原始 stderr。
    entry = {"time": int(time.time()), **result}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clean_text(text):
    # 剔除宿主注入的提醒、IDE 状态和附件包装，保留真实用户请求。
    for tag in ("system-reminder", "ide_selection", "ide_opened_file", "ide_diagnostics", "in-app-browser-context",
                "environment_context", "task-notification", "local-command-stdout", "local-command-caveat",
                "attachment", "skill"):
        text = re.sub(r"<" + tag + r"\b[^>]*>[\s\S]*?</" + tag + r">", "", text)
    return text.strip()


def project_hint(thread):
    cwd = thread.get("cwd")
    if not cwd:
        return ""
    path = Path(cwd)
    if path == Path.home() or path.parent.name.lower() in ("users", "home"):
        return ""
    name = path.name
    if name.lower() in ("desktop", "documents", "downloads", "tmp", "project", "projects"):
        return ""
    return name[:64] if re.fullmatch(r"[\w .-]{1,64}", name) else ""


def conflicting_titles(root, session_id, candidate, scope_key=""):
    conflicts = set()
    for path in (root / "sessions").glob("*.json"):
        if path.stem == session_id:
            continue
        try:
            state = read_json(path)
        except (ValueError, OSError):
            continue
        if state.get("scope_key", "") != scope_key:
            continue
        other = state.get("last_seen_title")
        if other == candidate:
            conflicts.add(other)
    return sorted(conflicts)


def snapshot(thread, config):
    """只给命名模型用户请求与最终回答；用最新记录 ID 检测生成期间的新活动。"""
    effective = []
    for turn in thread.get("turns", []):
        messages = []
        for message in turn.get("messages", []):
            text = clean_text(message.get("text", ""))
            if text:
                messages.append({"role": message["role"], "text": text})
        if any(m["role"] == "user" for m in messages):
            effective.append({"id": turn["id"], "messages": messages})
    title = thread.get("name") or ""
    selected = effective[-config["recent_turns"]:]
    budget = config["max_context_chars"]
    # 为每轮的用户目标保留空间；助手长回答不能挤掉其他轮。
    per_message = max(200, (budget - 2000) // max(1, sum(len(t["messages"]) for t in selected)))
    recent = [{"id": t["id"], "messages": [
        {"role": m["role"], "text": m["text"][:min(per_message, 1200 if m["role"] == "user" else 500)]}
        for m in t["messages"]
    ]} for t in selected]
    original = ""
    if effective:
        original = next(m["text"] for m in effective[0]["messages"] if m["role"] == "user")[:800]
    latest_id = thread.get("latest_id")
    context = {"current_title": title, "project_hint": project_hint(thread),
               "original_goal": original, "recent_turns": recent}
    signature = json.dumps({"policy_version": POLICY_VERSION, "project_hint": context["project_hint"],
                           "latest_id": latest_id, "effective": effective[-config["recent_turns"]:]},
                           ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(signature.encode()).hexdigest()
    scope_key = hashlib.sha256(str(thread["cwd"]).encode()).hexdigest() if thread.get("cwd") else ""
    return {"title": title, "latest_id": latest_id, "fingerprint": fingerprint,
            "context": context, "has_messages": bool(effective), "scope_key": scope_key}


def validate_candidate(candidate, current_title):
    if not isinstance(candidate, dict) or set(candidate) != {"action", "title", "reason"}:
        raise ValueError("模型输出字段无效")
    if candidate["action"] not in ("rename", "keep") or not all(
        isinstance(candidate[k], str) for k in ("title", "reason")
    ):
        raise ValueError("模型输出类型无效")
    if candidate["action"] == "keep":
        candidate = {**candidate, "title": current_title}
    else:
        title = candidate["title"]
        if title != title.strip() or not 4 <= len(title) <= 48:
            raise ValueError("标题长度或空白无效")
        if not any(title.startswith(e + " ") for e in EMOJI):
            raise ValueError("标题缺少允许的类别 emoji")
        body = title.split(" ", 1)[1]
        if body.count("｜") != 1 or "|" in body:
            raise ValueError("标题必须采用对象｜目标结构")
        if any(not part or part != part.strip() for part in body.split("｜")):
            raise ValueError("标题对象与目标不能为空或带边缘空格")
        if (not body.strip() or any(e in body for e in EMOJI)
                or any(0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF for c in body)):
            raise ValueError("标题正文无效或包含多个类别 emoji")
        if any(unicodedata.category(c).startswith("C") for c in title):
            raise ValueError("标题含控制字符")
        if re.search(r"[A-Za-z]:[\\/]|\\\\", title):
            raise ValueError("标题含 Windows 绝对路径")
        if any(x in title for x in ("\n", "\r", "`", "https://", "http://", "@", "/Users/", "/home/", "sk-")):
            raise ValueError("标题含不允许的格式或私人信息")
    return {**candidate, "reason": candidate["reason"][:300]}


def process_thread(backend, generator, session_id, root, config, *, apply=False, event_turn=None, transcript=None):
    session_id = valid_id(session_id)
    if not config["enabled"]:
        return {"status": "disabled"}
    # 新轮次的 Hook 等待旧 Worker 释放锁，再判断是否已经过期，避免丢掉最新请求。
    with thread_lock(root, session_id, config["model_timeout_seconds"] * 2 + 20 if event_turn else 0) as acquired:
        if not acquired:
            return {"status": "busy"}
        path = state_path(root, session_id)
        state = read_json(path)
        if event_turn:
            thread, pending = read_settled_thread(backend, session_id, transcript, event_turn)
            if pending:
                return {"status": pending}
        else:
            thread = backend.read(session_id, transcript)
        before = snapshot(thread, config)
        if not before["has_messages"]:
            return {"status": "empty"}
        # 上次写入后进程被中断时，先核对待确认结果，避免误认作手工改名。
        if state.get("pending_title") == before["title"]:
            state.update(last_seen_title=before["title"], last_generated_title=before["title"])
            state.pop("pending_title", None)
            if apply:
                atomic_json(path, state)
        # 初次观察到的标题可能仍是宿主自动生成的标题。只有成功评估/写入后，
        # 才有稳定基线可用于保护外部改名；过期结果不能建立这条基线。
        established = bool(state.get("last_fingerprint") or state.get("last_generated_title"))
        if (state.get("locked") and state.get("lock_reason") == "检测到外部改名"
                and not established):
            state.update(locked=False, lock_reason="首次标题尚未建立稳定基线")
            if apply:
                atomic_json(path, state)
                audit(root, session_id, {"status": "initial_baseline_recovered"})
        if state.get("locked"):
            return {"status": "locked", "title": before["title"]}
        if established and "last_seen_title" in state and state["last_seen_title"] != before["title"]:
            generated = state.get("last_generated_title")
            reverted = bool(generated) and state["last_seen_title"] == generated \
                and before["title"] == state.get("title_before_rename")
            if reverted and state.get("revert_count", 0) < 1:
                # 宿主（如桌面端）把自己缓存的旧标题写回了记录，不是用户改名：不上锁，重建基线，
                # 下轮照常评估；再次发生则视为宿主持续覆盖，交回宿主处理。
                if apply:
                    state.update(last_seen_title=before["title"], revert_count=state.get("revert_count", 0) + 1)
                    state.pop("last_fingerprint", None)
                    atomic_json(path, state)
                    audit(root, session_id, {"status": "host_reverted", "title": before["title"], "generated": generated})
                return {"status": "host_reverted", "title": before["title"], "generated": generated}
            if apply:
                state.update(locked=True, lock_reason="宿主反复写回旧标题" if reverted else "检测到外部改名",
                             last_seen_title=before["title"])
                atomic_json(path, state)
            return {"status": "manual_title", "title": before["title"]}
        if state.get("last_fingerprint") == before["fingerprint"]:
            return {"status": "unchanged", "title": before["title"]}
        try:
            ensure_title_active(session_id, root)
            candidate, usage = generator(before["context"])
        except ModelSkipped as exc:
            return {"status": exc.status}
        candidate = validate_candidate(candidate, before["title"])
        conflicts = conflicting_titles(root, session_id, candidate["title"], before["scope_key"])
        if candidate["action"] == "rename" and conflicts:
            try:
                ensure_title_active(session_id, root)
                candidate, retry_usage = generator({**before["context"], "conflicting_titles": conflicts,
                    "naming_feedback": "候选与已记录任务重名。用对话里真实的项目、模块或内容主题区分；无法区分就保留原名，不编造编号。"})
            except ModelSkipped as exc:
                return {"status": exc.status, "usage": usage}
            candidate = validate_candidate(candidate, before["title"])
            usage = {key: usage.get(key, 0) + retry_usage.get(key, 0)
                     for key in usage.keys() | retry_usage.keys()}
            if candidate["action"] == "rename" and conflicting_titles(root, session_id, candidate["title"], before["scope_key"]):
                return {"status": "ambiguous_title", "title": before["title"], "usage": usage}
        result = {"status": "preview", **candidate, "usage": usage}
        if not apply:
            return result
        # 模型运行期间用户可能发起下一轮、改名、暂停或锁定。
        if not load_config(root)["enabled"]:
            return {"status": "disabled"}
        if read_json(path).get("locked"):
            return {"status": "locked"}
        after = snapshot(backend.read(session_id, transcript), config)
        if after["title"] != before["title"] or after["fingerprint"] != before["fingerprint"]:
            return {"status": "stale_result"}
        state.update(last_seen_title=before["title"], last_turn_id=before["latest_id"],
                     scope_key=before["scope_key"], updated_at=int(time.time()))
        if candidate["action"] == "rename" and candidate["title"] != before["title"]:
            state["pending_title"] = candidate["title"]
            state["title_before_rename"] = before["title"]
            atomic_json(path, state)
            try:
                backend.rename(session_id, candidate["title"], transcript)
            except BackendError:
                # 写入可能已经落盘，先读回确认，不盲目重试。
                if snapshot(backend.read(session_id, transcript), config)["title"] != candidate["title"]:
                    raise
            verified = snapshot(backend.read(session_id, transcript), config)
            if verified["title"] != candidate["title"]:
                raise BackendError("标题写入后核验不一致")
            state.update(last_seen_title=candidate["title"], last_generated_title=candidate["title"])
            state.pop("pending_title", None)
            result["status"] = "renamed"
            result["verification"] = "metadata_only"
        else:
            result["status"] = "kept"
        state["last_fingerprint"] = before["fingerprint"]
        atomic_json(path, state)
        audit(root, session_id, result)
        return result


def hook_status():
    """只读宿主的插件登记；--plugin-dir 临时加载的插件不会出现在这里。"""
    home = config_home()
    try:
        settings = read_json(home / "settings.json")
        registry = read_json(home / "plugins" / "installed_plugins.json")
    except (ValueError, OSError):
        return {"status": "inspection_failed"}
    enabled = {key: value for key, value in (settings.get("enabledPlugins") or {}).items()
               if key.split("@")[0] == PLUGIN_NAME}
    installed = sorted(key for key in (registry.get("plugins") or {}) if key.split("@")[0] == PLUGIN_NAME)
    if enabled and all(enabled.values()):
        status = "ready"
    elif enabled or installed:
        status = "needs_enable"
    else:
        status = "not_installed"
    return {"status": status, "enabled": enabled, "installed": installed}


def doctor(binary, root, config, backend, session=None, transcript=None):
    version = subprocess.run([binary, "--version"], capture_output=True, encoding="utf-8", timeout=15,
                             **process_options())
    output = {"claude_bin": binary, "version": version.stdout.strip(), "python": platform.python_version(),
              "config": config, "data_dir": str(root), "projects_dir": str(backend.projects_dir),
              "hook": hook_status(), "desktop_display": "not_verified"}
    if session:
        thread = backend.read(valid_id(session), transcript)
        snap = snapshot(thread, config)
        output["session"] = {"title": snap["title"], "latest_id": snap["latest_id"],
                             "has_messages": snap["has_messages"], "turns": len(thread["turns"]),
                             "transcript": thread["path"]}
    return output


def detach(command):
    """派生独立进程组的后台 Worker；宿主不必等待模型返回。"""
    options = {"start_new_session": True}
    if sys.platform == "win32":
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                   | subprocess.CREATE_NEW_PROCESS_GROUP}
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **options)


def resolve_session(explicit):
    session_id = explicit or os.environ.get(SESSION_ENV)
    if not session_id:
        raise BackendError("缺少会话 ID；在 Claude Code 会话内运行会自动读取 CLAUDE_CODE_SESSION_ID，否则请显式指定")
    return valid_id(session_id)


def main():
    # Hook 事件使用 UTF-8；不能依赖 Windows 当前代码页解释中文内容。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立模型驱动的 Claude Code 会话命名")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="读取 Stop Hook stdin 并派生后台 Worker；保持宿主输出为空 JSON")
    p = sub.add_parser("worker", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True)
    p.add_argument("--transcript")
    p.add_argument("--prompt-id")
    p = sub.add_parser("doctor", help="只读检查运行环境")
    p.add_argument("--session")
    p.add_argument("--transcript")
    sub.add_parser("status", help="显示配置和本地记录数量")
    sub.add_parser("pause", help="暂停自动命名")
    sub.add_parser("resume", help="恢复自动命名")
    p = sub.add_parser("configure", help="配置独立命名模型或可执行文件")
    p.add_argument("--model")
    p.add_argument("--claude-bin")
    p.add_argument("--effort", choices=("low", "medium", "high"))
    p.add_argument("--thinking", choices=("on", "off"))
    for name in ("rename", "lock", "unlock"):
        p = sub.add_parser(name)
        p.add_argument("session_id", nargs="?", help="省略时读取 CLAUDE_CODE_SESSION_ID")
        p.add_argument("--transcript", help="会话记录路径；省略时按会话 ID 查找")
        if name == "rename":
            p.add_argument("--apply", action="store_true", help="写入；省略时只预览")
    args = parser.parse_args()
    root = data_dir()
    is_hook = args.command == "hook"
    is_worker = args.command == "worker"
    session_id = None
    try:
        if sys.version_info < (3, 9):
            raise BackendError("需要 Python 3.9 或更新版本")
        config = load_config(root)
        if is_hook:
            if os.environ.get("OIL_CLAUDE_TITLE_WORKER") == "1" or not config["enabled"]:
                return 0
            event = json.loads(sys.stdin.read(1024 * 1024))
            if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
                return 0
            session_id = valid_id(event["session_id"])
            transcript = event.get("transcript_path")
            if not isinstance(transcript, str) or not Path(transcript).is_file():
                return 0  # 未持久化的会话没有可读记录，也没有可显示的标题
            command = [sys.executable, str(Path(__file__).resolve()), "worker",
                       "--session", session_id, "--transcript", transcript]
            prompt_id = event.get("prompt_id")
            if isinstance(prompt_id, str) and re.fullmatch(r"[\w-]{1,64}", prompt_id):
                command += ["--prompt-id", prompt_id]
            detach(command)
            return 0
        if args.command in ("pause", "resume", "configure"):
            config_path = root / "config.json"
            changes = read_json(config_path)
            if args.command in ("pause", "resume"):
                changes["enabled"] = args.command == "resume"
            else:
                if args.model:
                    changes["model"] = args.model
                if args.claude_bin:
                    changes["claude_bin"] = find_claude(args.claude_bin)
                if args.effort:
                    changes["effort"] = args.effort
                if args.thinking:
                    changes["thinking"] = args.thinking == "on"
            atomic_json(config_path, changes)
            print(json.dumps(load_config(root), ensure_ascii=False))
            return 0
        if args.command == "status":
            print(json.dumps({"config": config, "data_dir": str(root),
                              "tracked_sessions": len(list((root / "sessions").glob("*.json")))}, ensure_ascii=False))
            return 0
        backend = TranscriptBackend()
        if args.command == "doctor":
            result = doctor(find_claude(config["claude_bin"]), root, config, backend, args.session, args.transcript)
        else:
            # 先解析会话再找可执行文件：缺会话 ID 的提示不应被「未找到 Claude」掩盖；lock/unlock 不需要模型。
            session_id = valid_id(args.session) if is_worker else resolve_session(args.session_id)
            transcript = args.transcript
            if args.command in ("lock", "unlock"):
                with thread_lock(root, session_id) as acquired:
                    if not acquired:
                        raise BackendError("该会话正在命名，稍后再试")
                    path = state_path(root, session_id)
                    state = read_json(path)
                    state.update(locked=args.command == "lock", lock_reason="用户设置",
                                 last_seen_title=backend.read(session_id, transcript).get("name") or "")
                    for key in ("last_fingerprint", "pending_title", "revert_count", "title_before_rename"):
                        state.pop(key, None)
                    atomic_json(path, state)
                    result = {"status": args.command, "title": state["last_seen_title"]}
            else:
                binary = find_claude(config["claude_bin"])
                result = process_thread(
                    backend, lambda context: limited_title(binary, root, config, context,
                        before_model=lambda: ensure_title_active(session_id, root)),
                    session_id, root, config, apply=is_worker or args.apply,
                    event_turn=args.prompt_id if is_worker else None, transcript=transcript,
                )
                if is_worker and result["status"] not in ("renamed", "kept"):
                    audit(root, session_id, result)
        if not is_worker:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = {"status": "error", "error_type": type(exc).__name__}
        if isinstance(exc, (BackendError, ValueError)):
            error["message"] = str(exc)[:300]
        if is_hook or is_worker:
            try:
                audit(root, session_id or "hook", error)
            except Exception:
                pass
            return 0
        if "message" not in error:
            error["message"] = "操作失败；检查输入及本地配置"
        print(json.dumps(error, ensure_ascii=False))
        return 1
    finally:
        if is_hook:
            print("{}")


if __name__ == "__main__":
    raise SystemExit(main())
