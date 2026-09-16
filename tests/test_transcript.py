"""会话记录解析、标题追加与临时模型进程参数的测试；不调用真实模型。"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from claude_adapter import BackendError, SCHEMA, TranscriptBackend, generate_json, message_text
import oil_claude_title as title

SID = "12345678-1234-1234-1234-123456789012"
P1, P2 = "aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002"


def rec(kind, content, **extra):
    record = {"type": kind, "uuid": extra.pop("uuid", str(uuid.uuid4())), "sessionId": SID, "isSidechain": False,
              "cwd": "/work/maple", "message": {"role": kind, "content": content}}
    record.update(extra)
    return record


def sample_lines():
    return [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": SID},
        rec("user", "帮我修复登录注册页的验证码过期问题", promptId=P1, uuid="u1"),
        {"type": "custom-title", "customTitle": "宿主自动标题", "sessionId": SID},
        rec("assistant", [{"type": "thinking", "thinking": "私密推理"}, {"type": "text", "text": "我先看一下代码"}], uuid="u2"),
        rec("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/work/maple/app.py"}}], uuid="u3"),
        rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "def login(): ..."}], uuid="u4", toolUseResult={}),
        rec("assistant", [{"type": "text", "text": "已修复：验证码过期改为返回 401"}], uuid="u5"),
        rec("user", [{"type": "text", "text": "Base directory for this skill: /x"}], isMeta=True, uuid="u6"),
        rec("user", "<command-name>/compact</command-name>", uuid="u7"),
        rec("user", "<local-command-stdout>Set model</local-command-stdout>", uuid="u8"),
        rec("user", "旁支任务请求", isSidechain=True, promptId="side", uuid="u9"),
        rec("assistant", [{"type": "text", "text": "旁支回答"}], isSidechain=True, uuid="u10"),
        rec("user", [{"type": "text", "text": "[Request interrupted by user]"}], uuid="u11"),
        rec("user", [{"type": "image", "source": {}}, {"type": "text", "text": "再补充单元测试"}], promptId=P2, uuid="u12"),
        rec("assistant", [{"type": "text", "text": "测试已添加"}], uuid="u13"),
        {"type": "custom-title", "customTitle": "🧩 邮箱注册｜验证码过期修复", "sessionId": SID},
    ]


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.folder = self.projects / "-work-maple"
        self.folder.mkdir(parents=True)
        self.path = self.folder / (SID + ".jsonl")
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sample_lines()), encoding="utf-8")
        self.backend = TranscriptBackend(self.projects)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_keeps_only_user_requests_and_final_answers(self):
        thread = self.backend.read(SID)
        self.assertEqual(thread["name"], "🧩 邮箱注册｜验证码过期修复")
        self.assertEqual(thread["cwd"], "/work/maple")
        self.assertEqual(thread["latest_id"], "u13")
        self.assertEqual([t["id"] for t in thread["turns"]], [P1, P2])
        self.assertEqual(thread["turns"][0]["messages"], [
            {"role": "user", "text": "帮我修复登录注册页的验证码过期问题"},
            {"role": "assistant", "text": "已修复：验证码过期改为返回 401"},
        ])
        self.assertEqual(thread["turns"][1]["messages"][1]["text"], "测试已添加")
        raw = json.dumps(thread, ensure_ascii=False)
        for hidden in ("私密推理", "def login", "Base directory", "/compact", "Set model", "旁支", "interrupted"):
            self.assertNotIn(hidden, raw)

    def test_locate_by_session_id_or_explicit_path_only(self):
        self.assertEqual(self.backend.locate(SID), self.path)
        self.assertEqual(self.backend.locate(SID, str(self.path)), self.path)
        with self.assertRaises(BackendError):
            self.backend.locate("00000000-0000-4000-8000-000000000000")
        with self.assertRaises(BackendError):
            self.backend.locate(SID, str(self.folder / "missing.jsonl"))

    def test_rename_appends_host_compatible_record_without_rewriting(self):
        before = self.path.read_text(encoding="utf-8")
        self.backend.rename(SID, "🧩 邮箱注册｜测试补充")
        after = self.path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertEqual(json.loads(after[len(before):]), {"type": "custom-title", "customTitle": "🧩 邮箱注册｜测试补充", "sessionId": SID})
        self.assertEqual(self.backend.read(SID)["name"], "🧩 邮箱注册｜测试补充")

    def test_rename_seals_torn_tail_before_appending(self):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"assistant","uuid":"torn')
        self.assertEqual(self.backend.read(SID)["latest_id"], "u13")
        self.backend.rename(SID, "🧩 邮箱注册｜尾行修复")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["customTitle"], "🧩 邮箱注册｜尾行修复")
        self.assertEqual(lines[-2], '{"type":"assistant","uuid":"torn')
        self.assertEqual(self.backend.read(SID)["name"], "🧩 邮箱注册｜尾行修复")

    def test_rename_updates_existing_sidecar_only(self):
        self.backend.rename(SID, "🧩 邮箱注册｜无侧车")
        self.assertFalse((self.folder / SID).exists())
        sidecar = self.folder / SID / "custom-title.json"
        sidecar.parent.mkdir()
        sidecar.write_text('{"customTitle":"旧"}', encoding="utf-8")
        self.backend.rename(SID, "🧩 邮箱注册｜有侧车")
        self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")), {"customTitle": "🧩 邮箱注册｜有侧车"})

    def test_foreign_session_titles_are_ignored(self):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "custom-title", "customTitle": "别的会话", "sessionId": "other"}) + "\n")
        self.assertEqual(self.backend.read(SID)["name"], "🧩 邮箱注册｜验证码过期修复")

    def test_message_text_shapes(self):
        self.assertEqual(message_text("a"), "a")
        self.assertEqual(message_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "a\nb")
        self.assertIsNone(message_text([{"type": "tool_result", "content": "x"}]))
        self.assertIsNone(message_text(None))

    def test_end_to_end_process_uses_real_transcript_backend(self):
        root = Path(self.tmp.name) / "data"
        def generator(context):
            self.assertEqual(context["project_hint"], "maple")
            self.assertEqual(context["current_title"], "🧩 邮箱注册｜验证码过期修复")
            self.assertEqual(len(context["recent_turns"]), 2)
            return {"action": "rename", "title": "🧩 邮箱注册｜验证码修复与测试", "reason": "补充测试"}, {"input_tokens": 1}
        result = title.process_thread(self.backend, generator, SID, root, title.DEFAULTS, apply=True, event_turn=P2)
        self.assertEqual(result["status"], "renamed")
        self.assertEqual(self.backend.read(SID)["name"], "🧩 邮箱注册｜验证码修复与测试")
        self.assertEqual(title.process_thread(self.backend, generator, SID, root, title.DEFAULTS, apply=True)["status"], "unchanged")


class HeadlessModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = Path(self.tmp.name) / "naming.md"
        self.policy.write_text("规则", encoding="utf-8")
        self.config = {**title.DEFAULTS, "model_timeout_seconds": 30}
        self.context = {"current_title": "旧标题", "original_goal": "修复中文工具", "recent_turns": []}

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **overrides):
        base = {"structured_output": {"action": "rename", "title": "🧩 中文工具｜修复", "reason": "目标明确"},
                "usage": {"input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 400},
                "total_cost_usd": 0.0123456, "is_error": False, "permission_denials": []}
        base.update(overrides)
        return SimpleNamespace(returncode=0, stdout=json.dumps(base, ensure_ascii=False))

    def test_headless_invocation_is_isolated_and_tool_free(self):
        captured = {}
        def fake_run(args, **kwargs):
            captured.update(args=args, **kwargs)
            return self.payload()
        with patch("claude_adapter.subprocess.run", side_effect=fake_run):
            result, usage = generate_json("/bin/claude", self.config, self.context, self.policy, SCHEMA)
        args = captured["args"]
        self.assertEqual(args[:2], ["/bin/claude", "-p"])
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(args[args.index("--system-prompt-file") + 1], str(self.policy))
        self.assertEqual(json.loads(captured["input"])["original_goal"], "修复中文工具")
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["env"]["OIL_CLAUDE_TITLE_WORKER"], "1")
        self.assertEqual(captured["env"]["MAX_THINKING_TOKENS"], "0")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", captured["env"])
        self.assertEqual(result["title"], "🧩 中文工具｜修复")
        self.assertEqual(usage["total_cost_usd"], 0.012346)
        self.assertEqual(usage["input_tokens"], 5)

    def test_thinking_switch_and_model_override(self):
        captured = {}
        def fake_run(args, **kwargs):
            captured.update(args=args, **kwargs)
            return self.payload()
        with patch("claude_adapter.subprocess.run", side_effect=fake_run):
            generate_json("/bin/claude", {**self.config, "thinking": True, "model": "sonnet", "effort": "medium"},
                          self.context, self.policy, SCHEMA)
        self.assertNotIn("MAX_THINKING_TOKENS", captured["env"])
        self.assertEqual(captured["args"][captured["args"].index("--model") + 1], "sonnet")
        self.assertEqual(captured["args"][captured["args"].index("--effort") + 1], "medium")

    def test_plain_result_json_is_accepted_when_structured_output_missing(self):
        with patch("claude_adapter.subprocess.run", return_value=self.payload(
                structured_output=None, result=json.dumps({"action": "keep", "title": "", "reason": "不变"}))):
            result, _ = generate_json("/bin/claude", self.config, self.context, self.policy, SCHEMA)
        self.assertEqual(result["action"], "keep")

    def test_failures_never_yield_a_candidate(self):
        cases = {
            "exit_code": SimpleNamespace(returncode=1, stdout=""),
            "not_json": SimpleNamespace(returncode=0, stdout="oops"),
            "is_error": self.payload(is_error=True),
            "tool_attempt": self.payload(permission_denials=[{"tool_name": "Bash"}]),
            "no_structure": self.payload(structured_output=None, result="纯文本"),
        }
        for name, response in cases.items():
            with self.subTest(name=name), patch("claude_adapter.subprocess.run", return_value=response):
                with self.assertRaises(BackendError):
                    generate_json("/bin/claude", self.config, self.context, self.policy, SCHEMA)

    def test_before_model_runs_after_queue_and_before_process(self):
        order = []
        def fake_run(args, **kwargs):
            order.append("run")
            return self.payload()
        with patch("claude_adapter.subprocess.run", side_effect=fake_run):
            generate_json("/bin/claude", self.config, self.context, self.policy, SCHEMA,
                          before_model=lambda: order.append("check"))
        self.assertEqual(order, ["check", "run"])


if __name__ == "__main__":
    unittest.main()
