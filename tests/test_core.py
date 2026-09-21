"""TokenFurnace 核心单元测试 —— 全部离线，不依赖网络。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokenfurnace.config import ConfigStore, new_profile  # noqa: E402
from tokenfurnace.engine import (Engine, Live, RunSpec, Session,  # noqa: E402
                               bump_minute, bump_recent, effective_window,
                               instant_rate, spark_series)
from tokenfurnace.providers import (AUTH_STYLES, PROTOCOLS, Adapter,  # noqa: E402
                                  classify, guess_protocol, join_url)
from tokenfurnace.store import Store  # noqa: E402


# ====================================================================== #
class TestUrlJoin(unittest.TestCase):
    def test_appends_v1_when_missing(self):
        self.assertEqual(join_url("https://api.x.com", "/chat/completions"),
                         "https://api.x.com/v1/chat/completions")

    def test_keeps_existing_v1(self):
        self.assertEqual(join_url("https://api.x.com/v1", "/chat/completions"),
                         "https://api.x.com/v1/chat/completions")

    def test_keeps_nonstandard_version_segment(self):
        self.assertEqual(join_url("https://x.com/api/paas/v4", "/chat/completions"),
                         "https://x.com/api/paas/v4/chat/completions")

    def test_strips_pasted_endpoint(self):
        for pasted in ("https://api.x.com/v1/chat/completions",
                       "https://api.x.com/v1/messages",
                       "https://api.x.com/v1/responses",
                       "https://api.x.com/v1/models"):
            self.assertEqual(join_url(pasted, "/chat/completions"),
                             "https://api.x.com/v1/chat/completions", pasted)

    def test_trailing_slash(self):
        self.assertEqual(join_url("https://api.x.com/v1/", "/models"),
                         "https://api.x.com/v1/models")

    def test_gemini_endpoint_is_normalised(self):
        path = "/models/{model}:generateContent"
        for base in ("https://generativelanguage.googleapis.com",
                     "https://generativelanguage.googleapis.com/v1beta",
                     "https://generativelanguage.googleapis.com/v1beta/models",
                     "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"):
            self.assertEqual(
                join_url(base, path, "v1beta"),
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                base)

    def test_empty_base(self):
        self.assertEqual(join_url("", "/models"), "/models")


# ====================================================================== #
class TestClassify(unittest.TestCase):
    """错误分类是核心安全逻辑，必须区分限流和真额度耗尽。"""

    def test_rps_masquerading_as_quota(self):
        body = json.dumps({"error": {"message": "rps exhausted",
                                     "type": "quota_exceeded_error"}})
        kind, _ = classify(429, body)
        self.assertEqual(kind, "rps", "type 写着 quota，但 message 是 rps，必须判为限流")

    def test_real_quota_exhausted(self):
        body = json.dumps({"error": {"message": "token plan entitlement exhausted",
                                     "type": "quota_exceeded_error"}})
        kind, _ = classify(429, body)
        self.assertEqual(kind, "quota")

    def test_insufficient_balance(self):
        body = json.dumps({"error": {"message": "Insufficient balance"}})
        self.assertEqual(classify(402, body)[0], "quota")

    def test_auth(self):
        self.assertEqual(classify(401, "{}")[0], "auth")
        self.assertEqual(classify(403, "{}")[0], "auth")

    def test_not_found_mentions_protocol(self):
        kind, msg = classify(404, "{}")
        self.assertEqual(kind, "bad_request")
        self.assertIn("协议", msg)

    def test_server_error(self):
        self.assertEqual(classify(503, "")[0], "server")

    def test_rate_limit_wording(self):
        self.assertEqual(classify(429, '{"error":{"message":"Rate limit reached"}}')[0], "rps")


# ====================================================================== #
class TestAdapterBuild(unittest.TestCase):
    def _adapter(self, proto):
        return Adapter(proto, "https://api.example.com/v1", "sk-test", None, 30)

    def test_openai_chat(self):
        path, p = self._adapter("openai-chat").build("m", "SYS", "USER", 8, "none")
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(p["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(p["messages"][1]["content"], "USER")
        self.assertEqual(p["max_tokens"], 8)
        self.assertNotIn("stream", [k for k in p if k != "stream"] or [])
        self.assertFalse(p["stream"])

    def test_openai_responses_uses_instructions_and_input(self):
        path, p = self._adapter("openai-responses").build("m", "SYS", "USER", 8, "none")
        self.assertEqual(path, "/responses")
        self.assertEqual(p["instructions"], "SYS")
        self.assertEqual(p["input"], "USER")
        self.assertEqual(p["max_output_tokens"], 8)
        self.assertNotIn("messages", p)
        self.assertNotIn("max_tokens", p)

    def test_anthropic_system_is_toplevel(self):
        path, p = self._adapter("anthropic").build("m", "SYS", "USER", 8, "none")
        self.assertEqual(path, "/messages")
        self.assertEqual(p["system"], "SYS")
        self.assertEqual(p["messages"], [{"role": "user", "content": "USER"}])
        # system 不能出现在 messages 里
        self.assertFalse([m for m in p["messages"] if m.get("role") == "system"])

    def test_gemini_shape(self):
        path, p = self._adapter("gemini").build("m", "SYS", "USER", 8, "none")
        self.assertEqual(path, "/models/{model}:generateContent")
        self.assertEqual(p["systemInstruction"], {"parts": [{"text": "SYS"}]})
        self.assertEqual(p["contents"], [{"role": "user", "parts": [{"text": "USER"}]}])
        self.assertEqual(p["generationConfig"]["maxOutputTokens"], 8)
        self.assertNotIn("messages", p)
        self.assertNotIn("max_tokens", p)

    def test_gemini_thinking_config(self):
        _, p = self._adapter("gemini").build("m", "", "U", 8, "high")
        self.assertIn("thinkingConfig", p["generationConfig"])
        self.assertGreater(p["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)

    def test_gemini_model_path_has_single_prefix(self):
        a = self._adapter("gemini")
        self.assertEqual(a._model_for_path("gemini-2.0-flash"), "gemini-2.0-flash")
        self.assertEqual(a._model_for_path("models/gemini-2.0-flash"), "gemini-2.0-flash")

    def test_gemini_uses_v1beta(self):
        self.assertEqual(self._adapter("gemini").spec["api_version"], "v1beta")
        self.assertEqual(self._adapter("openai-chat").spec["api_version"], "v1")

    def test_anthropic_thinking_budget(self):
        _, p = self._adapter("anthropic").build("m", "", "U", 8, "high")
        self.assertEqual(p["thinking"]["type"], "enabled")
        self.assertGreater(p["thinking"]["budget_tokens"], 0)
        # 开了思考，max_tokens 必须留出预算，否则会被 API 拒绝
        self.assertGreater(p["max_tokens"], p["thinking"]["budget_tokens"])

    def test_responses_reasoning_effort_maps_max_to_high(self):
        _, p = self._adapter("openai-responses").build("m", "", "U", 8, "max")
        self.assertEqual(p["reasoning"]["effort"], "high")

    def test_auth_headers_per_protocol(self):
        self.assertIn("Authorization", self._adapter("openai-chat").headers)
        a = self._adapter("anthropic").headers
        self.assertIn("x-api-key", a)
        self.assertIn("anthropic-version", a)
        self.assertNotIn("Authorization", a)
        g = self._adapter("gemini").headers
        self.assertIn("x-goog-api-key", g)
        self.assertNotIn("Authorization", g)

    def test_every_auth_style_puts_key_in_the_right_header(self):
        from tokenfurnace.providers import AUTH_STYLES
        self.assertEqual(set(AUTH_STYLES), {"bearer", "x-api-key", "x-goog-api-key"})
        for style, spec in AUTH_STYLES.items():
            a = Adapter("openai-chat", "https://x.com", "SECRET", None, 30, auth_style=style)
            hdr = spec["header"]
            self.assertEqual(a.headers.get(hdr), spec["format"].format(key="SECRET"),
                             f"{style} 的凭证头不对")
        # 密钥为空时不应发出任何凭证头——所以不需要单独的「不认证」选项
        a = Adapter("openai-chat", "https://x.com", "", None, 30)
        self.assertNotIn("Authorization", a.headers)

    def test_unknown_auth_style_falls_back_to_protocol_default(self):
        """认证字段写错时回落到协议默认值，而不是变成不认证。"""
        a = Adapter("openai-chat", "https://x.com", "SECRET", None, 30, auth_style="nonsense")
        self.assertIn("Authorization", a.headers)
        b = Adapter("anthropic", "https://x.com", "SECRET", None, 30, auth_style="nonsense")
        self.assertIn("x-api-key", b.headers)

    def test_custom_auth_style_override(self):
        a = Adapter("openai-chat", "https://x.com/v1", "k", None, 30, auth_style="x-api-key")
        self.assertIn("x-api-key", a.headers)
        self.assertNotIn("Authorization", a.headers)

    def test_no_key_no_auth_header(self):
        a = Adapter("openai-chat", "https://x.com/v1", "", None, 30)
        self.assertNotIn("Authorization", a.headers)


# ====================================================================== #
class TestAdapterParse(unittest.TestCase):
    def test_chat_completions_shape(self):
        r = Adapter.parse({"model": "m", "usage": {
            "prompt_tokens": 10, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 1}}})
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"],
                          r["cached_tokens"], r["reasoning_tokens"]), (10, 4, 2, 1))

    def test_responses_shape(self):
        r = Adapter.parse({"model": "m", "usage": {
            "input_tokens": 100, "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens_details": {"reasoning_tokens": 3}}})
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"],
                          r["cached_tokens"], r["reasoning_tokens"]), (100, 5, 7, 3))

    def test_anthropic_shape(self):
        r = Adapter.parse({"model": "m", "usage": {
            "input_tokens": 200, "output_tokens": 9, "cache_read_input_tokens": 3}})
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"],
                          r["cached_tokens"]), (200, 9, 3))

    def test_gemini_usage_metadata(self):
        r = Adapter.parse({"modelVersion": "gemini-2.0-flash", "usageMetadata": {
            "promptTokenCount": 1234, "candidatesTokenCount": 56,
            "thoughtsTokenCount": 7, "cachedContentTokenCount": 9}})
        self.assertEqual(r["model"], "gemini-2.0-flash")
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"],
                          r["cached_tokens"], r["reasoning_tokens"]), (1234, 56, 9, 7))

    def test_empty_usage_is_safe(self):
        r = Adapter.parse({})
        self.assertEqual(r["prompt_tokens"], 0)
        self.assertEqual(r["completion_tokens"], 0)


# ====================================================================== #
class TestGuessProtocol(unittest.TestCase):
    def test_anthropic_hosts(self):
        self.assertEqual(guess_protocol("https://api.anthropic.com"), "anthropic")
        self.assertEqual(guess_protocol("https://my-claude-relay.io/v1"), "anthropic")

    def test_gemini_hosts(self):
        self.assertEqual(guess_protocol("https://generativelanguage.googleapis.com"), "gemini")
        self.assertEqual(guess_protocol("https://my-gemini-proxy.dev/v1beta"), "gemini")

    def test_default_is_chat(self):
        self.assertEqual(guess_protocol("https://api.openai.com/v1"), "openai-chat")
        self.assertEqual(guess_protocol(""), "openai-chat")

    def test_protocol_table_is_complete(self):
        from tokenfurnace.providers import AUTH_STYLES, PROTOCOLS
        self.assertEqual(set(PROTOCOLS),
                         {"anthropic", "openai-chat", "openai-responses", "gemini"})
        for k, v in PROTOCOLS.items():
            self.assertIn(v["auth"], AUTH_STYLES, f"{k} 的默认认证字段不存在")
            for f in ("label", "short", "path", "api_version", "hint"):
                self.assertTrue(v.get(f), f"{k} 缺少 {f}")


# ====================================================================== #
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_are_provider_agnostic(self):
        c = ConfigStore(self.path)
        d = c.load()
        self.assertEqual(len(d["profiles"]), 1)
        p = d["profiles"][0]
        self.assertEqual(p["base_url"], "")
        self.assertEqual(p["protocol"], "openai-chat")
        self.assertEqual(p["models"], [])
        # 出厂配置里不能出现任何具体服务商的名字
        blob = json.dumps(d, ensure_ascii=False).lower()
        for brand in ("sensenova", "openai.com", "anthropic.com", "deepseek", "moonshot"):
            self.assertNotIn(brand, blob, f"默认配置不应硬编码 {brand}")

    def test_key_source_inline(self):
        prof = new_profile()
        prof["auth"] = {"source": "inline", "value": "sk-abc", "style": "bearer"}
        self.assertEqual(ConfigStore.resolve_key(prof), "sk-abc")

    def test_key_source_env(self):
        os.environ["TF_TEST_KEY"] = "sk-from-env"
        try:
            prof = new_profile()
            prof["auth"] = {"source": "env", "ref": "TF_TEST_KEY", "style": "bearer"}
            self.assertEqual(ConfigStore.resolve_key(prof), "sk-from-env")
        finally:
            os.environ.pop("TF_TEST_KEY", None)

    def test_key_source_env_missing(self):
        prof = new_profile()
        prof["auth"] = {"source": "env", "ref": "TF_DOES_NOT_EXIST_XYZ", "style": "bearer"}
        self.assertEqual(ConfigStore.resolve_key(prof), "")

    def test_key_source_file(self):
        f = Path(self.tmp.name) / "key.txt"
        f.write_text("sk-in-file\n", encoding="utf-8")
        prof = new_profile()
        prof["auth"] = {"source": "file", "ref": str(f), "style": "bearer"}
        self.assertEqual(ConfigStore.resolve_key(prof), "sk-in-file")

    def test_mask_key_hides_middle(self):
        m = ConfigStore.mask_key("sk-1234567890abcdef")
        self.assertIn("*", m)
        self.assertTrue(m.startswith("sk-123"))
        self.assertTrue(m.endswith("cdef"))
        self.assertNotIn("567890ab", m)

    def test_public_view_never_leaks_inline_key(self):
        c = ConfigStore(self.path)
        c.load()
        p = c.data["profiles"][0]
        p["auth"] = {"source": "inline", "value": "sk-SUPERSECRET123456",
                     "ref": "", "style": "bearer"}
        blob = json.dumps(c.public_view(), ensure_ascii=False)
        self.assertNotIn("SUPERSECRET", blob)

    def test_save_and_reload_roundtrip(self):
        c = ConfigStore(self.path)
        c.load()
        c.data["profiles"][0]["base_url"] = "https://example.com/v1"
        c.data["profiles"][0]["protocol"] = "anthropic"
        c.save()
        c2 = ConfigStore(self.path)
        d = c2.load()
        self.assertEqual(d["profiles"][0]["base_url"], "https://example.com/v1")
        self.assertEqual(d["profiles"][0]["protocol"], "anthropic")

    def test_migrates_v1_config(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "active_profile": "old",
            "profiles": [{"id": "old", "name": "老配置",
                          "base_url": "https://x.com/v1",
                          "auth": {"source": "inline", "value": "sk-old"}}],
        }), encoding="utf-8")
        c = ConfigStore(self.path)
        d = c.load()
        p = d["profiles"][0]
        self.assertEqual(d["version"], 2)
        self.assertEqual(p["protocol"], "openai-chat")
        self.assertEqual(p["auth"]["style"], "bearer")
        self.assertEqual(p["auth"]["value"], "sk-old")

    def test_env_injection_creates_profile(self):
        os.environ["TOKENFURNACE_BASE_URL"] = "https://injected.example/v1"
        os.environ["TOKENFURNACE_PROTOCOL"] = "anthropic"
        try:
            c = ConfigStore(self.path)
            d = c.load()
            self.assertEqual(d["active_profile"], "__env__")
            p = c.get_profile()
            self.assertEqual(p["base_url"], "https://injected.example/v1")
            self.assertEqual(p["protocol"], "anthropic")
        finally:
            os.environ.pop("TOKENFURNACE_BASE_URL", None)
            os.environ.pop("TOKENFURNACE_PROTOCOL", None)

    def test_import_profiles(self):
        c = ConfigStore(self.path)
        c.load()
        n = c.import_profiles([
            {"name": "A", "base_url": "https://a.com/v1"},
            {"name": "B", "base_url": "https://b.com/v1", "protocol": "anthropic"},
            {"nonsense": True},
        ])
        self.assertEqual(n, 2)
        self.assertEqual(len(c.data["profiles"]), 3)

    def test_delete_profile_keeps_active_valid(self):
        c = ConfigStore(self.path)
        c.load()
        c.add_profile("temp")
        c.data["active_profile"] = c.data["profiles"][-1]["id"]
        c.delete_profile(c.data["profiles"][-1]["id"])
        self.assertTrue(c.get_profile())

    def test_find_profile_is_strict(self):
        """启动会话必须用严格查找：宽松查找会静默回退到第一个配置，
        结果就是你以为在跑配置 X，实际烧的是配置 A 的额度。"""
        c = ConfigStore(self.path)
        c.load()
        c.add_profile("第二个")
        second = c.data["profiles"][-1]["id"]
        self.assertIsNotNone(c.find_profile(second))
        self.assertIsNone(c.find_profile("不存在的id"))
        # 宽松查找的旧行为保留给展示用
        self.assertIsNotNone(c.get_profile("不存在的id"))


# ====================================================================== #
class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.run_id = self.store.create_run("p", "P", ["m1"], "prefill", 2)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_window(self):
        now = time.time()
        self.store.add_request(self.run_id, "m1", 100, 10, 0, 0.5, now)
        self.store.add_request(self.run_id, "m1", 200, 20, 0, 0.5, now - 4000)
        self.assertEqual(self.store.window_tokens(3600), 110)
        self.assertEqual(self.store.window_tokens(7200), 330)

    def test_window_by_model(self):
        now = time.time()
        self.store.add_request(self.run_id, "m1", 100, 0, 0, 0.1, now)
        self.store.add_request(self.run_id, "m2", 500, 0, 0, 0.1, now)
        self.assertEqual(self.store.window_tokens(3600, model="m1"), 100)
        self.assertEqual(self.store.window_tokens(3600, model="m2"), 500)

    def test_points_are_locked_in_at_insert_time(self):
        """窗口积分取落库值，不该被后来的系数改动影响。"""
        now = time.time()
        self.store.add_request(self.run_id, "m1", 1000, 0, 0, 0.1, now, points=1.4)
        self.store.add_request(self.run_id, "m2", 1000, 0, 0, 0.1, now, points=2.8)
        self.assertAlmostEqual(self.store.window_points(3600), 4.2, places=6)

    def test_window_points_cover_all_models_not_just_current_selection(self):
        """换模型选择不该让历史用量从窗口里消失。"""
        now = time.time()
        self.store.add_request(self.run_id, "selected-model", 1000, 0, 0, 0.1, now, points=1.0)
        self.store.add_request(self.run_id, "other-model", 5000, 0, 0, 0.1, now, points=7.0)
        self.assertAlmostEqual(self.store.window_points(3600), 8.0, places=6)
        self.assertAlmostEqual(self.store.window_points(3600), 8.0, places=6)

    def test_window_points_respects_time_window(self):
        now = time.time()
        self.store.add_request(self.run_id, "m1", 1000, 0, 0, 0.1, now, points=5.0)
        self.store.add_request(self.run_id, "m1", 1000, 0, 0, 0.1, now - 4000, points=9.0)
        self.assertAlmostEqual(self.store.window_points(3600), 5.0, places=6)

    def test_backfill_only_touches_rows_without_points(self):
        now = time.time()
        self.store.add_request(self.run_id, "m1", 1000, 0, 0, 0.1, now, points=0)
        self.assertEqual(self.store.backfill_points(1.4), 1)
        self.assertAlmostEqual(self.store.window_points(3600), 1.4, places=6)
        self.assertEqual(self.store.backfill_points(1.4), 0, "已回填过就不该重复回填")

    def test_errors_do_not_count_toward_tokens(self):
        self.store.add_error(self.run_id, "m1", "rps", "too fast")
        self.assertEqual(self.store.window_tokens(3600), 0)
        stats = self.store.run_stats(self.run_id)
        self.assertEqual(stats["failed"], 1)

    def test_run_totals(self):
        self.store.add_request(self.run_id, "m1", 100, 10, 0, 0.5)
        self.store.add_request(self.run_id, "m1", 200, 20, 0, 0.5)
        s = self.store.run_stats(self.run_id)
        self.assertEqual(s["prompt_tokens"], 300)
        self.assertEqual(s["completion_tokens"], 30)
        self.assertEqual(s["requests"], 2)

    def test_finish_run(self):
        self.store.finish_run(self.run_id, "done", "到时长")
        s = self.store.run_stats(self.run_id)
        self.assertEqual(s["status"], "done")
        self.assertEqual(s["note"], "到时长")
        self.assertIsNotNone(s["ended"])

    def test_stale_runs_marked_interrupted(self):
        self.store.mark_stale_runs()
        self.assertEqual(self.store.run_stats(self.run_id)["status"], "interrupted")

    def test_model_breakdown_and_errors(self):
        self.store.add_request(self.run_id, "m1", 100, 0, 0, 0.1)
        self.store.add_request(self.run_id, "m2", 300, 0, 0, 0.1)
        self.store.add_error(self.run_id, "m1", "rps", "x")
        self.store.add_error(self.run_id, "m1", "rps", "x")
        mb = self.store.model_breakdown(self.run_id)
        self.assertEqual(mb[0]["model"], "m2")
        self.assertEqual(self.store.error_breakdown(self.run_id)[0]["n"], 2)

    def test_export_csv_has_header_and_rows(self):
        self.store.add_request(self.run_id, "m1", 1, 2, 0, 0.1)
        csv = self.store.export_requests_csv(self.run_id)
        lines = csv.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("模型", lines[0])
        self.assertEqual(len(self.store.export_runs_csv().strip().splitlines()), 2)


# ====================================================================== #
class TestEngineRate(unittest.TestCase):
    """即时速率 / 迷你图 / 累计，都是纯函数，离线可测。"""

    def setUp(self):
        self.L = Live()

    def test_instant_rate_over_window(self):
        now = time.time()
        for i in range(10):                     # 20 秒内 10 次，每次 1000 token
            bump_recent(self.L, now - i * 2, 1000, 20.0)
        self.assertAlmostEqual(instant_rate(self.L, now, 20.0), 1000 * 10 / 20, delta=1)

    def test_instant_decays_when_idle(self):
        now = time.time()
        bump_recent(self.L, now - 100, 5000, 20.0)   # 100 秒前，早已滑出 20 秒窗口
        self.assertEqual(instant_rate(self.L, now, 20.0), 0)

    def test_shorter_window_reads_higher(self):
        """同一份数据，窗口越短读到的瞬时值越高——这正是可调窗口的意义。"""
        now = time.time()
        for i in range(30):
            bump_recent(self.L, now - i * 10, 180000, 5.0)
        self.assertGreater(instant_rate(self.L, now, 5.0),
                           instant_rate(self.L, now, 60.0))

    def test_recent_is_trimmed(self):
        now = time.time()
        for i in range(500):
            bump_recent(self.L, now - 400 + i, 10, 20.0)
        self.assertLess(len(self.L.recent), 500, "超出保留窗口的记录必须被裁掉")

    def test_peak_tracks_maximum(self):
        now = time.time()
        for i in range(10):
            bump_recent(self.L, now - i, 1000, 20.0)
        self.assertGreater(self.L.rate_peak, 0)

    def test_spark_length_and_zero_fill(self):
        self.assertEqual(len(spark_series(self.L, time.time(), 20.0)), 40)
        self.assertEqual(len(spark_series(self.L, time.time(), 5.0)), 40)

    def test_window_widens_when_completions_are_sparse(self):
        """预填充模式单请求 20~40 秒，5 秒窗口会长期读成 0——必须自动放宽。"""
        now = time.time()
        for i in range(6):                      # 每 30 秒完成一次，按时间正序
            bump_recent(self.L, now - (5 - i) * 30, 180000, 5.0)
        eff = effective_window(self.L.recent, now, 5.0)
        self.assertGreater(eff, 5.0, "完成间隔 30 秒，窗口必须被放宽")
        self.assertGreater(instant_rate(self.L, now, eff), 0,
                           "放宽之后不该再读到 0")

    def test_window_stays_put_when_completions_are_dense(self):
        now = time.time()
        for i in range(20):                     # 每 1 秒完成一次，按时间正序
            bump_recent(self.L, now - (19 - i), 1000, 20.0)
        self.assertEqual(effective_window(self.L.recent, now, 20.0), 20.0)

    def test_window_not_widened_without_enough_samples(self):
        now = time.time()
        bump_recent(self.L, now, 1000, 5.0)     # 只有一次完成
        self.assertEqual(effective_window(self.L.recent, now, 5.0), 5.0)

    def test_backoff_is_global_for_rps_but_per_thread_for_timeout(self):
        """rps 是全局状况（所有线程一起停）；timeout 通常只是某条连接卡住，
        全局停会白白浪费并发额度——只能停当前线程。"""
        s = Session("t", RunSpec(concurrency=4))
        s._backoff("timeout", 0)
        self.assertEqual(s._pause_until, 0.0, "timeout 不该触发全局暂停")
        self.assertGreater(s._tls()["pause"], time.time(), "timeout 应停本线程")
        s._backoff("rps", 0)
        self.assertGreater(s._pause_until, time.time(), "rps 应触发全局暂停")

    def test_adaptive_timeout_tightens_to_observed_latency(self):
        """固定 180 秒意味着一条挂住的连接占住 worker 三分钟，太浪费。"""
        s = Session("t", RunSpec(timeout=180))
        s._adapter = Adapter("openai-chat", "https://x.com", "k", None, 180)
        for _ in range(5):
            s._tune_timeout(25.0)               # 实测稳定在 25 秒
        self.assertLess(s._adapter.timeout, 100, "应按实测延迟收紧")
        self.assertGreaterEqual(s._adapter.timeout, 30, "但要有下限")

    def test_adaptive_timeout_never_exceeds_user_setting(self):
        s = Session("t", RunSpec(timeout=60))
        s._adapter = Adapter("openai-chat", "https://x.com", "k", None, 60)
        for _ in range(5):
            s._tune_timeout(90.0)               # 实测比用户设的还慢
        self.assertLessEqual(s._adapter.timeout, 60)

    def test_per_minute_bucketing(self):
        base = int(time.time() // 60) * 60
        bump_minute(self.L, base + 1, 10)
        bump_minute(self.L, base + 30, 5)
        bump_minute(self.L, base + 61, 7)
        self.assertEqual(len(self.L.per_minute), 2)
        self.assertEqual(self.L.per_minute[0]["tokens"], 15)
        self.assertEqual(self.L.per_minute[1]["tokens"], 7)


class TestRunSpec(unittest.TestCase):
    def test_from_dict_ignores_unknown_keys(self):
        s = RunSpec.from_dict({"mode": "decode", "concurrency": 8, "bogus": 1})
        self.assertEqual(s.mode, "decode")
        self.assertEqual(s.concurrency, 8)
        self.assertFalse(hasattr(s, "bogus"))

    def test_output_length_and_token_budget_are_separate_fields(self):
        s = RunSpec.from_dict({"max_tokens": 8, "max_total_tokens": 5_000_000})
        self.assertEqual(s.max_tokens, 8)
        self.assertEqual(s.max_total_tokens, 5_000_000)

    def test_budget_stops(self):
        s = Session("t", RunSpec(max_requests=3))
        s.live.ok = 3
        self.assertIn("请求数", s._check_budgets())
        s.spec = RunSpec(max_total_tokens=100)
        s.live.ok = 0
        s.live.prompt_tokens, s.live.completion_tokens = 60, 50
        self.assertIn("token", s._check_budgets())
        s.spec = RunSpec(max_points=10)
        s.live.prompt_tokens = s.live.completion_tokens = 0
        s.live.points = 11
        self.assertIn("积分", s._check_budgets())

    def test_no_budget_means_no_stop(self):
        s = Session("t", RunSpec())
        self.assertEqual(s._check_budgets(), "")


class TestMultiSession(unittest.TestCase):
    """多配置并发：这是「能不能多个配置一起跑」的核心保障。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")
        self.e = Engine(self.store, max_total_workers=10)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _profile(self, name, pid):
        return {"id": pid, "name": name, "protocol": "openai-chat",
                "base_url": "https://example.invalid/v1", "auth": {}}

    def _spec(self, concurrency=2, models=("m1",)):
        return RunSpec(models=[{"id": m, "weight": 1, "points_per_1k": 0} for m in models],
                       concurrency=concurrency, mode="prefill",
                       input_chars=1000, enforce_windows=False)

    def test_can_start_multiple_sessions(self):
        r1 = self.e.start(self._spec(), self._profile("A", "pa"), "k")
        r2 = self.e.start(self._spec(), self._profile("B", "pb"), "k")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertNotEqual(r1["sid"], r2["sid"], "两个会话必须是不同 id")
        self.assertEqual(len(self.e.sessions()), 2)
        self.e.stop_all("测试结束")

    def test_worker_cap_is_enforced(self):
        ok = self.e.start(self._spec(concurrency=6), self._profile("A", "pa"), "k")
        self.assertTrue(ok["ok"])
        over = self.e.start(self._spec(concurrency=6), self._profile("B", "pb"), "k")
        self.assertFalse(over["ok"], "超出全局 worker 上限时必须拒绝")
        self.assertIn("并发额度不足", over["error"])
        self.e.stop_all("测试结束")

    def test_rejects_missing_models_and_base_url(self):
        self.assertFalse(self.e.start(RunSpec(models=[]),
                                      self._profile("A", "pa"), "k")["ok"])
        self.assertFalse(self.e.start(self._spec(),
                                      {"name": "x", "base_url": ""}, "k")["ok"])

    def test_aggregate_sums_all_sessions(self):
        s1 = Session("s1", self._spec(), self._profile("A", "pa"))
        s2 = Session("s2", self._spec(), self._profile("B", "pb"))
        for s, tok in ((s1, 1000), (s2, 2000)):
            s.live.requests = 1
            s.live.ok = 1
            s.live.prompt_tokens = tok
            s.live.points = tok / 1000.0
            bump_recent(s.live, time.time(), tok, 20.0)
        agg = self.e._aggregate([s1.snapshot(), s2.snapshot()])
        self.assertEqual(agg["total_tokens"], 3000)
        self.assertEqual(agg["requests"], 2)
        self.assertAlmostEqual(agg["points"], 3.0, places=6)
        self.assertGreater(agg["rate_instant"], 0)
        self.assertEqual(len(agg["spark"]), 40)

    def test_empty_aggregate_is_safe(self):
        agg = self.e._aggregate([])
        self.assertEqual(agg["total_tokens"], 0)
        self.assertFalse(agg["running"])

    def test_snapshot_carries_session_list(self):
        s = Session("s1", self._spec(models=("m1", "m2")), self._profile("A", "pa"))
        with self.e.lock:
            self.e._sessions["s1"] = s
        snap = self.e.snapshot()
        self.assertEqual(snap["session_count"], 1)
        self.assertEqual(snap["sessions"][0]["name"], "A")
        self.assertEqual(snap["sessions"][0]["models"], ["m1", "m2"])
        self.assertNotIn("recent", snap["sessions"][0], "raw recent 不应出现在响应里")

    def test_stop_one_leaves_others_running(self):
        r1 = self.e.start(self._spec(), self._profile("A", "pa"), "k")
        r2 = self.e.start(self._spec(), self._profile("B", "pb"), "k")
        self.e.stop(r1["sid"], "只停这个")
        self.e.stop_all("清理")
        self.assertTrue(r1["ok"] and r2["ok"])

    def test_set_instant_window_is_clamped(self):
        self.assertEqual(self.e.set_instant_window(0), 20.0)
        self.assertEqual(self.e.set_instant_window(1), 2.0)
        self.assertEqual(self.e.set_instant_window(9999), 300.0)
        self.assertEqual(self.e.set_instant_window(10), 10.0)

    def test_stop_reports_stopping_not_stopped(self):
        """在途 HTTP 请求杀不掉（Python 没法强杀线程），所以停止后状态必须是
        「正在停止」而不是假装已经停了——否则用户以为停了、其实还在烧 token。"""
        s = Session("s1", self._spec(), self._profile("A", "pa"))
        s._thread = threading.Thread(target=lambda: time.sleep(0.4))
        s._thread.start()
        s.stop("测试停止")
        self.assertEqual(s.live.status, "stopping")
        self.assertEqual(s.live.stop_reason, "测试停止")
        self.assertTrue(s.running, "线程还活着，running 就应该是 True")
        s.wait(2)
        self.assertFalse(s.running)

    def test_aggregate_status_is_stopping_when_all_are_stopping(self):
        s1 = Session("s1", self._spec(), self._profile("A", "pa"))
        s2 = Session("s2", self._spec(), self._profile("B", "pb"))
        for s in (s1, s2):
            s.live.status = "stopping"
            s._thread = threading.Thread(target=lambda: time.sleep(0.3))
            s._thread.start()
        agg = self.e._aggregate([s1.snapshot(), s2.snapshot()])
        self.assertEqual(agg["status"], "stopping")
        self.assertTrue(agg["running"])
        for s in (s1, s2):
            s.wait(2)

    def test_stop_all_shares_one_deadline(self):
        """三个会话不能各等 30 秒——总等待时间必须被 grace 夹住。"""
        sess = [Session(f"s{i}", self._spec(), self._profile(f"P{i}", f"p{i}"))
                for i in range(3)]
        for s in sess:
            s._thread = threading.Thread(target=lambda: time.sleep(0.6))
            s._thread.start()
        with self.e.lock:
            for s in sess:
                self.e._sessions[s.sid] = s
        t0 = time.time()
        self.e.stop_all("测试", grace=0.2)
        self.assertLess(time.time() - t0, 1.5, "总等待时间应被 grace 夹住，而不是累加")
        for s in sess:
            s.wait(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
