"""测试跨进程命名中的写入边界、保护规则和 Hook 输出契约。"""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_claude_title as title
from claude_adapter import BackendError, worker_env, generate_title, normalize_project_prefix

ID = "12345678-1234-1234-1234-123456789012"
TURN = "12345678-1234-1234-1234-123456789013"
NEW_TURN = "12345678-1234-1234-1234-123456789014"


def thread():
    return {"name": "讨论事情", "cwd": None, "latest_id": TURN, "turns": [
        {"id": TURN, "messages": [
            {"role": "user", "text": "请为产品设计视频讲解大纲"},
            {"role": "assistant", "text": "视频大纲已整理"},
        ]},
    ]}


class FakeBackend:
    def __init__(self):
        self.thread = thread()
        self.writes = []
        self.raise_after_write = False

    def read(self, session_id, transcript=None):
        return copy.deepcopy(self.thread)

    def rename(self, session_id, text, transcript=None):
        self.writes.append((session_id, text))
        self.thread["name"] = text
        if self.raise_after_write:
            raise BackendError("写入后的连接中断")


def proposal(_):
    return {"action": "rename", "title": "🎬 产品视频｜讲解大纲", "reason": "主要目标已明确"}, {"input_tokens": 100}


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.config = title.DEFAULTS.copy()
        self.settle = patch.object(title, "SETTLE_TIMEOUT", 0.05)
        self.settle.start()

    def tearDown(self):
        self.settle.stop()
        self.tmp.cleanup()

    def process(self, generator=proposal, **kwargs):
        return title.process_thread(self.backend, generator, ID, self.root, self.config, **kwargs)

    def test_language_titles_survive_validation_write_and_readback(self):
        for new_title in (
            "🧩 Email verification｜Fix expiry",
            "🎨 ログイン画面｜余白調整",
            "🧩 Verificación｜Corregir caducidad",
            "🧩 邮箱验证码｜过期修复",
        ):
            with self.subTest(title=new_title):
                self.backend.thread["name"] = "待整理"
                title.state_path(self.root, ID).unlink(missing_ok=True)
                candidate = {"action": "rename", "title": new_title, "reason": "语言迁移"}
                result = self.process(generator=lambda _: (candidate, {}), apply=True)
                self.assertEqual(result["status"], "renamed")
                self.assertEqual(self.backend.read(ID)["name"], new_title)

    def test_preview_never_changes_title_or_history(self):
        before = copy.deepcopy(self.backend.thread)
        self.assertEqual(self.process()["status"], "preview")
        self.assertEqual(self.backend.thread, before)
        self.assertFalse(title.state_path(self.root, ID).exists())

    def test_pause_prevents_collision_retry(self):
        from unittest.mock import Mock
        title.atomic_json(self.root / "config.json", {"enabled": True})
        title.atomic_json(title.state_path(self.root, "12345678-1234-1234-1234-123456789099"),
                          {"scope_key": "", "last_seen_title": proposal({})[0]["title"]})
        def paused(context):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            return proposal(context)
        model = Mock(side_effect=paused)
        self.assertEqual(self.process(model, apply=True)["status"], "disabled")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.backend.writes, [])

    def test_pause_prevents_internal_format_retry(self):
        from types import SimpleNamespace
        title.atomic_json(self.root / "config.json", {"enabled": True})
        def fake_run(args, **kwargs):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            payload = {"structured_output": {"action": "keep", "title": "旧标题", "reason": "格式合规"},
                       "usage": {"input_tokens": 1}, "total_cost_usd": 0.001}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        def model(context):
            return title.limited_title("unused", self.root, self.config, context,
                before_model=lambda: title.ensure_title_active(ID, self.root))
        with patch("claude_adapter.subprocess.run", side_effect=fake_run) as run:
            result = self.process(model, apply=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.backend.writes, [])

    def test_pause_while_waiting_for_naming_slot_never_starts_model(self):
        from contextlib import contextmanager
        @contextmanager
        def queued(*args):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            yield True
        def model(context):
            return title.limited_title("unused", self.root, self.config, context,
                before_model=lambda: title.ensure_title_active(ID, self.root))
        with patch.object(title, "worker_slot", queued), patch("claude_adapter.subprocess.run") as run:
            result = self.process(model, apply=True)
        run.assert_not_called()
        self.assertEqual(result["status"], "disabled")

    def test_apply_renames_only_metadata_and_deduplicates(self):
        turns = copy.deepcopy(self.backend.thread["turns"])
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertEqual(self.backend.thread["turns"], turns)
        def forbidden(_):
            self.fail("重复内容不应调用模型")
        self.assertEqual(self.process(forbidden, apply=True)["status"], "unchanged")
        self.assertEqual(len(self.backend.writes), 1)

    def test_external_manual_title_is_locked(self):
        self.process(apply=True)
        self.backend.thread["name"] = "我的固定标题"
        result = self.process(apply=True)
        self.assertEqual(result["status"], "manual_title")
        self.assertTrue(title.read_json(title.state_path(self.root, ID))["locked"])
        self.assertEqual(self.backend.thread["name"], "我的固定标题")

    def test_host_revert_to_previous_title_is_tolerated_once(self):
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.backend.thread["name"] = "讨论事情"  # 宿主把改名前的旧标题写回
        result = self.process(apply=True)
        self.assertEqual((result["status"], result["generated"]), ("host_reverted", "🎬 产品视频｜讲解大纲"))
        self.assertFalse(title.read_json(title.state_path(self.root, ID)).get("locked"))
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.backend.thread["name"] = "讨论事情"
        self.assertEqual(self.process(apply=True)["status"], "manual_title")
        self.assertEqual(title.read_json(title.state_path(self.root, ID))["lock_reason"], "宿主反复写回旧标题")
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_genuine_new_external_title_still_locks_immediately(self):
        self.process(apply=True)
        self.backend.thread["name"] = "用户自己起的新标题"
        self.assertEqual(self.process(apply=True)["status"], "manual_title")
        self.assertEqual(title.read_json(title.state_path(self.root, ID))["lock_reason"], "检测到外部改名")

    def test_explicit_lock_skips_model(self):
        title.atomic_json(title.state_path(self.root, ID), {"locked": True})
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_new_turn_during_model_discards_result(self):
        def moved(context):
            self.backend.thread["turns"].append({"id": NEW_TURN, "messages": [{"role": "user", "text": "换个话题"}]})
            self.backend.thread["latest_id"] = NEW_TURN
            return proposal(context)
        self.assertEqual(self.process(moved, apply=True)["status"], "stale_result")
        self.assertEqual(self.backend.writes, [])

    def test_first_run_host_title_change_does_not_create_manual_lock(self):
        def moved(context):
            self.backend.thread["name"] = "宿主生成的首轮标题"
            return proposal(context)
        self.assertEqual(self.process(moved, apply=True)["status"], "stale_result")
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertFalse(title.read_json(title.state_path(self.root, ID)).get("locked"))

    def test_greeting_then_request_and_delayed_host_title(self):
        self.backend.thread["name"] = ""
        self.backend.thread["turns"][0]["messages"] = [
            {"role": "user", "text": "你好"}, {"role": "assistant", "text": "你好"},
        ]
        def next_turn(context):
            self.backend.thread["name"] = "回应中文问候"
            self.backend.thread["turns"].append({"id": NEW_TURN, "messages": [{"role": "user", "text": "查看本地 Skill"}]})
            self.backend.thread["latest_id"] = NEW_TURN
            return proposal(context)
        self.assertEqual(self.process(next_turn, apply=True, event_turn=TURN)["status"], "stale_result")
        self.assertEqual(self.process(apply=True, event_turn=NEW_TURN)["status"], "renamed")

    def test_recovers_only_legacy_unestablished_automatic_lock(self):
        path = title.state_path(self.root, ID)
        title.atomic_json(path, {"last_seen_title": "讨论事情", "locked": True,
                                 "lock_reason": "检测到外部改名"})
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        state = title.read_json(path)
        state.update(locked=True, lock_reason="检测到外部改名")
        title.atomic_json(path, state)
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_explicit_initial_lock_is_not_migrated(self):
        title.atomic_json(title.state_path(self.root, ID), {"last_seen_title": "讨论事情",
                          "locked": True, "lock_reason": "用户设置"})
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_unknown_event_turn_does_not_call_model(self):
        self.assertEqual(self.process(lambda _: self.fail(), apply=True, event_turn=NEW_TURN)["status"], "turn_not_settled")

    def test_stale_hook_for_superseded_turn_does_not_call_model(self):
        self.backend.thread["turns"].append({"id": NEW_TURN, "messages": [{"role": "user", "text": "下一轮"}]})
        self.assertEqual(self.process(lambda _: self.fail(), apply=True, event_turn=TURN)["status"], "outdated_event")

    def test_pause_during_generation_prevents_write(self):
        def paused(context):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            return proposal(context)
        self.assertEqual(self.process(paused, apply=True)["status"], "disabled")
        self.assertEqual(self.backend.writes, [])

    def test_bad_model_output_never_writes(self):
        for bad in ("🎬 正常\n恶意换行", "没有 emoji", "🎬 标题 📝", "🎬 ‮反向控制", "🎬 a@b.com", "🧩 /Users/x｜修复"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.process(lambda _: ({"action": "rename", "title": bad, "reason": ""}, {}), apply=True)
        self.assertEqual(self.backend.writes, [])

    def test_keep_never_replaces_title(self):
        result = self.process(lambda _: ({"action": "keep", "title": "模型误改", "reason": "保持"}, {}), apply=True)
        self.assertEqual(result["status"], "kept")
        self.assertEqual(result["title"], "讨论事情")
        self.assertEqual(self.backend.writes, [])

    def test_lost_write_response_is_verified_without_retry(self):
        self.backend.raise_after_write = True
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertEqual(len(self.backend.writes), 1)

    def test_nested_lock_skips_duplicate_worker(self):
        with title.thread_lock(self.root, ID):
            self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "busy")

    def test_new_hook_waits_for_old_worker_then_checks_latest_turn(self):
        result = []
        with title.thread_lock(self.root, ID):
            worker = threading.Thread(target=lambda: result.append(self.process(apply=True, event_turn=TURN)))
            worker.start()
            time.sleep(0.05)
            self.assertEqual(result, [])
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "renamed")

    def test_context_omits_host_reminders_and_ide_state(self):
        messages = self.backend.thread["turns"][0]["messages"]
        messages[0]["text"] = ('<system-reminder>不可作为目标的提醒</system-reminder>实际请求'
                               '<ide_selection>选中的代码</ide_selection>')
        context = title.snapshot(self.backend.thread, self.config)["context"]
        raw = json.dumps(context, ensure_ascii=False)
        self.assertIn("实际请求", raw)
        self.assertNotIn("不可作为目标", raw)
        self.assertNotIn("选中的代码", raw)
        self.assertEqual(context["original_goal"], "实际请求")

    def test_project_hint_avoids_home_and_full_paths(self):
        self.assertEqual(title.project_hint({'cwd': '/workspace/maple'}), 'maple')
        self.assertEqual(title.project_hint({'cwd': str(Path.home())}), '')
        self.assertEqual(title.project_hint({'cwd': '/Users/example'}), '')
        self.assertEqual(title.project_hint({'cwd': '/workspace/projects'}), '')

    def test_duplicate_candidate_retries_with_evidence(self):
        title.atomic_json(title.state_path(self.root, NEW_TURN), {'last_seen_title': '🎬 产品视频｜讲解大纲'})
        contexts = []
        def generate(context):
            contexts.append(context)
            if len(contexts) == 1:
                return proposal(context)
            return {'action': 'rename', 'title': '🎬 Maple 产品入门视频｜大纲', 'reason': '补充产品名'}, {'input_tokens': 80}
        result = self.process(generate, apply=True)
        self.assertEqual(result['status'], 'renamed')
        self.assertEqual(result['usage']['input_tokens'], 180)
        self.assertIn('conflicting_titles', contexts[1])
        self.assertEqual(len(self.backend.writes), 1)

    def test_unresolved_duplicate_never_writes(self):
        title.atomic_json(title.state_path(self.root, NEW_TURN), {'last_seen_title': '🎬 产品视频｜讲解大纲'})
        self.assertEqual(self.process(apply=True)['status'], 'ambiguous_title')
        self.assertEqual(self.backend.writes, [])

    def test_same_title_in_different_project_is_allowed(self):
        self.backend.thread['cwd'] = '/workspace/maple'
        title.atomic_json(title.state_path(self.root, NEW_TURN), {
            'last_seen_title': '🎬 产品视频｜讲解大纲', 'scope_key': 'other-project'})
        calls = []
        def generate(context):
            calls.append(context)
            return proposal(context)
        self.assertEqual(self.process(generate, apply=True)['status'], 'renamed')
        self.assertEqual(len(calls), 1)
        state = title.read_json(title.state_path(self.root, ID))
        self.assertNotIn('/workspace/', json.dumps(state))
        self.assertEqual(len(state['scope_key']), 64)

    def test_arbitrary_second_emoji_is_rejected(self):
        with self.assertRaises(ValueError):
            title.validate_candidate({'action': 'rename', 'title': '🛠️ Maple 注册修复 🚀', 'reason': ''}, '')

    def test_failed_hook_returns_only_empty_json(self):
        env = os.environ | {"OIL_CLAUDE_TITLE_DATA": str(self.root)}
        env.pop("OIL_CLAUDE_TITLE_WORKER", None)
        for payload in ("not json", json.dumps({"hook_event_name": "Stop", "session_id": "../../escape"})):
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_claude_title.py"), "hook"],
                                  input=payload, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "{}\n")
            self.assertEqual(proc.stderr, "")

    def test_worker_guard_does_not_start_recursive_model(self):
        env = os.environ | {"OIL_CLAUDE_TITLE_DATA": str(self.root), "OIL_CLAUDE_TITLE_WORKER": "1"}
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_claude_title.py"), "hook"],
                              input="ignored", capture_output=True, text=True, env=env)
        self.assertEqual(proc.stdout, "{}\n")
        self.assertFalse((self.root / "logs").exists())
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", worker_env())
        self.assertEqual(worker_env()["OIL_CLAUDE_TITLE_WORKER"], "1")

    def run_hook(self, event, env=None):
        env = {"OIL_CLAUDE_TITLE_DATA": str(self.root), **(env or {})}
        with patch.dict(os.environ, env), patch.object(sys, "argv", ["oil_claude_title.py", "hook"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(title, "detach") as spawn, patch("sys.stdout", new_callable=io.StringIO) as out:
            os.environ.pop("OIL_CLAUDE_TITLE_WORKER", None)
            code = title.main()
        return code, spawn, out.getvalue()

    def test_hook_spawns_detached_worker_with_event_fields(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = {"hook_event_name": "Stop", "session_id": ID, "transcript_path": str(transcript),
                 "prompt_id": TURN, "stop_hook_active": False, "cwd": str(self.root)}
        code, spawn, out = self.run_hook(event)
        self.assertEqual((code, out), (0, "{}\n"))
        command = spawn.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[2:], ["worker", "--session", ID, "--transcript", str(transcript), "--prompt-id", TURN])

    def test_hook_ignores_continuations_other_events_and_unsaved_sessions(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        base = {"hook_event_name": "Stop", "session_id": ID, "transcript_path": str(transcript), "stop_hook_active": False}
        for event in ({**base, "stop_hook_active": True}, {**base, "hook_event_name": "SubagentStop"},
                      {**base, "transcript_path": str(self.root / "missing.jsonl")}, {**base, "transcript_path": None}):
            with self.subTest(event=event):
                code, spawn, out = self.run_hook(event)
                self.assertEqual((code, out), (0, "{}\n"))
                spawn.assert_not_called()

    def test_greeting_only_never_starts_model_process(self):
        candidate, usage = generate_title('/does-not-exist', self.config, {
            'current_title': '回应中文问候', 'original_goal': '你好！',
            'recent_turns': [{'messages': [{'role': 'user', 'text': '你好'}]}]}, ROOT)
        self.assertEqual(candidate['action'], 'keep')
        self.assertEqual(usage, {})

    def test_outer_project_prefix_normalizes_only_exact_identity(self):
        p = {'action': 'rename', 'title': '🎨 Kite LMS 课程详情页加载优化', 'reason': ''}
        self.assertEqual(normalize_project_prefix(p, {'project_hint':'kite-lms'})['title'], '🎨 课程详情页加载优化')
        self.assertEqual(normalize_project_prefix(p, {'project_hint':''}), p)

    def test_subproject_and_content_names_are_preserved(self):
        for hint, text in [('commerce-suite','🛠️ seller-console 订单导出修复'),
                           ('rednote','🔎 H3 与 H3 Max 模型评测'),
                           ('maple','🛠️ MaplePay 支付修复'),
                           ('maple','🛠️ Maple 与 Cedar 注册同步')]:
            p = {'action':'rename','title':text,'reason':''}
            self.assertEqual(normalize_project_prefix(p, {'project_hint':hint}), p)

    def test_kept_project_prefix_is_not_cleaned_up(self):
        p = {'action':'keep','title':'🛠️ Maple 注册修复','reason':''}
        self.assertEqual(normalize_project_prefix(p, {'project_hint':'maple'}), p)

    def test_structured_title_rejects_incomplete_or_multiple_parts(self):
        for bad in ("🧩 邮箱注册修复", "🧩 邮箱注册|修复", "🧩 ｜修复",
                    "🧩 邮箱注册｜", "🧩 邮箱注册｜修复｜测试", "🧩 邮箱注册 ｜修复",
                    "🛠️ 邮箱注册｜修复"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                title.validate_candidate({"action": "rename", "title": bad, "reason": ""}, "")

    def test_legacy_keep_is_allowed_but_new_tool_category_is_valid(self):
        old = "🛠️ 邮箱注册修复"
        result = title.validate_candidate({"action": "keep", "title": "", "reason": "信息不足"}, old)
        self.assertEqual(result["title"], old)
        new = "🧩 邮箱注册｜修复"
        self.assertEqual(title.validate_candidate({"action": "rename", "title": new, "reason": ""}, old)["title"], new)

    def test_policy_upgrade_rechecks_history_once_without_bypassing_locks(self):
        with patch.object(title, "POLICY_VERSION", title.POLICY_VERSION - 1):
            self.process(apply=True)
        calls = []
        def migrate(context):
            calls.append(context)
            return {"action": "keep", "title": context["current_title"], "reason": "准确"}, {}
        self.assertEqual(self.process(migrate, apply=True)["status"], "kept")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.process(migrate, apply=True)["status"], "unchanged")
        self.assertEqual(len(calls), 1)

    def test_legacy_keep_gets_one_bounded_format_review(self):
        context = {"current_title": "🔎 本地 Skill 清单梳理"}
        old = {"action": "keep", "title": context["current_title"], "reason": "结构合规"}
        new = {"action": "rename", "title": "📝 本地 Skill｜清单梳理", "reason": "格式迁移"}
        with patch("claude_adapter._generate_title_once", side_effect=[(old, {"input_tokens": 10}), (new, {"input_tokens": 20})]) as call:
            candidate, usage = generate_title("unused", {}, context, ROOT)
        self.assertEqual(candidate, new)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(call.call_count, 2)
        self.assertIn("naming_feedback", call.call_args.args[2])

    def test_format_review_does_not_force_uncertain_keep_or_loop(self):
        keep = {"action": "keep", "title": "待定", "reason": "信息不足"}
        with patch("claude_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call:
            candidate, _ = generate_title("unused", {}, {"current_title": "待定"}, ROOT)
        self.assertEqual(candidate, keep)
        self.assertEqual(call.call_count, 2)

    def test_structured_keep_needs_no_format_retry(self):
        keep = {"action": "keep", "title": "📝 页面还原｜方法整理", "reason": "主线准确"}
        with patch("claude_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call:
            generate_title("unused", {}, {"current_title": keep["title"]}, ROOT)
        self.assertEqual(call.call_count, 1)

    def test_format_review_shares_model_deadline(self):
        keep = {"action": "keep", "title": "旧标题", "reason": ""}
        with patch("claude_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call, \
                patch("claude_adapter.time.monotonic", side_effect=[0, 90]):
            generate_title("unused", {"model_timeout_seconds": 100}, {"current_title": "旧标题"}, ROOT)
        self.assertEqual(call.call_args.args[1]["model_timeout_seconds"], 10)
        with patch("claude_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call, \
                patch("claude_adapter.time.monotonic", side_effect=[0, 101]):
            generate_title("unused", {"model_timeout_seconds": 100}, {"current_title": "旧标题"}, ROOT)
        self.assertEqual(call.call_count, 1)

    def test_global_worker_slots_limit_different_sessions_and_release(self):
        with title.worker_slot(self.root, 2, 0) as first:
            with title.worker_slot(self.root, 2, 0) as second:
                with title.worker_slot(self.root, 2, 0) as third:
                    self.assertTrue(first)
                    self.assertTrue(second)
                    self.assertFalse(third)
            with title.worker_slot(self.root, 2, 0) as available:
                self.assertTrue(available)

    def test_global_queue_exhaustion_never_calls_model(self):
        config = {**self.config, "max_parallel_workers": 1, "model_timeout_seconds": 0}
        with title.worker_slot(self.root, 1, 0):
            with patch.object(title, "generate_title") as call, self.assertRaises(BackendError):
                title.limited_title("unused", self.root, config, {})
        call.assert_not_called()

    def test_stop_waits_for_flushed_answer_instead_of_fixed_sleep(self):
        running = self.backend.read(ID)
        running["turns"][-1]["messages"] = running["turns"][-1]["messages"][:1]
        with patch.object(self.backend, "read", side_effect=[running, self.backend.thread]) as read, \
                patch.object(title.time, "sleep"):
            settled, status = title.read_settled_thread(self.backend, ID, None, TURN, timeout=1)
        self.assertIsNone(status)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(settled["turns"][-1]["messages"]), 2)

    def test_config_validation_covers_new_fields(self):
        for bad in ({"effort": "max"}, {"thinking": "yes"}, {"max_budget_usd": 0}, {"max_budget_usd": True}):
            with self.subTest(bad=bad):
                title.atomic_json(self.root / "config.json", bad)
                with self.assertRaises(ValueError):
                    title.load_config(self.root)
        title.atomic_json(self.root / "config.json", {"model": "sonnet", "thinking": True, "unknown": 1})
        config = title.load_config(self.root)
        self.assertEqual((config["model"], config["thinking"], config["unknown"]), ("sonnet", True, 1))


if __name__ == "__main__":
    unittest.main()
