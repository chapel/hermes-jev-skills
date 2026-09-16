import copy
import unittest
from unittest.mock import Mock, patch

from jev_skills import engine


SKILLS = [
    {"name": "video", "description": "Make a video.  Preserve this spacing.\n"},
    {"name": "comfyui", "description": "Generate images, video, and audio — exact text."},
    {"name": "audio", "description": "Produce audio."},
]


def response():
    return {
        "model": "jev-fixture",
        "answers": {
            "video": {"type": "noul", "noul": 0.8},
            "comfyui": {"type": "noul", "noul": 0.8},
            "audio": {"type": "noul", "noul": 0},
        },
        "usage": {"input_tokens": 123, "output_tokens": 12},
        "request_id": "fixture-request",
    }


class EngineTests(unittest.TestCase):
    def test_self_contained_questions_tiny_state_and_stable_ranking(self):
        requester = Mock(return_value=response())
        original = copy.deepcopy(SKILLS)
        result = engine.rank_skills(
            "Make a video", SKILLS, api_key="fixture-key", requester=requester,
            model="jev-requested", timeout=1.25,
        )
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual([s["name"] for s in result["scores"]], ["comfyui", "video", "audio"])
        self.assertEqual([s["probability"] for s in result["scores"]], [0.8, 0.8, 0])
        self.assertEqual(result["model"], "jev-fixture")
        self.assertEqual(result["usage"], response()["usage"])
        self.assertEqual(result["request_id"], "fixture-request")
        self.assertGreaterEqual(result["latency_ms"], 0)
        requester.assert_called_once()
        payload, key, timeout = requester.call_args.args
        self.assertEqual((key, timeout), ("fixture-key", 1.25))
        self.assertEqual(set(payload), {"model", "state", "questions"})
        self.assertEqual(payload["model"], "jev-requested")
        self.assertEqual(payload["state"], {"user_request": "Make a video"})
        self.assertEqual(set(payload["questions"]), {s["name"] for s in SKILLS})
        for skill in SKILLS:
            question = payload["questions"][skill["name"]]
            self.assertEqual(question["type"], "noul")
            self.assertIn(skill["name"], question["instructions"])
            self.assertIn(skill["description"], question["instructions"])
            self.assertEqual(set(question["criteria"]), {"true", "false"})
            self.assertIn("alternative", question["criteria"]["true"].lower())
            ranked = next(s for s in result["scores"] if s["name"] == skill["name"])
            self.assertEqual(ranked["description"], skill["description"])
        self.assertEqual(SKILLS, original)

    def test_missing_answer_is_an_error_not_zero(self):
        data = response()
        del data["answers"]["audio"]
        requester = Mock(return_value=data)
        result = engine.rank_skills("Make a video", SKILLS, api_key="fixture-key", requester=requester)
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result["error"]["code"], "protocol_error")
        self.assertNotIn("scores", result)
        requester.assert_called_once()

    def test_empty_catalog_never_calls_transport(self):
        requester = Mock()
        result = engine.rank_skills("hello", [], api_key="", requester=requester)
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result["scores"], [])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["model"], "jev-latest")
        requester.assert_not_called()

    def test_recent_context_is_optional_and_verbatim(self):
        for context in (None, [], [{"role": "user", "content": "  previous request\n"}]):
            with self.subTest(context=context):
                requester = Mock(return_value=response())
                original = copy.deepcopy(context)
                result = engine.rank_skills("  current request\n", SKILLS,
                                           api_key="fixture-key", recent_context=context,
                                           requester=requester)
                self.assertEqual(result["status"], "ok")
                state = requester.call_args.args[0]["state"]
                expected: dict = {"user_request": "  current request\n"}
                if context is not None:
                    expected["recent_context"] = context
                self.assertEqual(state, expected)
                self.assertEqual(context, original)

    def test_absent_or_null_usage_stays_unknown_and_stats_are_ignored(self):
        for usage in ("absent", None, {}):
            with self.subTest(usage=usage):
                data = response()
                del data["request_id"]
                data["stats"] = {"irrelevant": "ignored"}
                if usage == "absent":
                    del data["usage"]
                else:
                    data["usage"] = usage
                result = engine.rank_skills("video", SKILLS, api_key="fixture-key",
                                           requester=Mock(return_value=data))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["usage"], None if usage == "absent" else usage)
                self.assertNotIn("request_id", result)
                self.assertNotIn("stats", result)

    def test_malformed_responses_are_protocol_errors(self):
        variants = [None, [], "private request", {}, {"model": "fixture", "answers": []}]
        for field, values in {
            "model": [None, "", "  ", 7, False],
            "usage": [[], "private request", False, 7],
            "request_id": [None, "", "  ", [], False],
        }.items():
            for value in values:
                data = response()
                data[field] = value
                variants.append(data)
        data = response()
        data["answers"]["unexpected"] = {"type": "noul", "noul": 0.5}
        variants.append(data)
        for answer in (None, [], {}, {"noul": 0.5}, {"type": "choice", "noul": 0.5}):
            data = response()
            data["answers"]["video"] = answer
            variants.append(data)
        for probability in (None, True, False, "0.5", [], -0.1, 1.1,
                            float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            data = response()
            data["answers"]["video"]["noul"] = probability
            variants.append(data)
        for data in variants:
            with self.subTest(response=data):
                requester = Mock(return_value=data)
                result = engine.rank_skills("private request", SKILLS,
                                           api_key="secret-fixture-key", requester=requester)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "protocol_error")
                self.assertNotIn("scores", result)
                self.assertNotIn("private request", str(result))
                self.assertNotIn("secret-fixture-key", str(result))
                requester.assert_called_once()

    def test_zero_and_one_are_valid_probabilities(self):
        data = response()
        data["answers"]["video"]["noul"] = 1
        result = engine.rank_skills("video", SKILLS, api_key="fixture-key",
                                   requester=Mock(return_value=data))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scores"][0]["probability"], 1)
        self.assertEqual(result["scores"][-1]["probability"], 0)

    def test_invalid_local_inputs_do_not_call_requester(self):
        cycles = []
        cycles.append({"content": cycles})
        cases = [
            {"query": value} for value in (None, "", "  ", 7, [], "\ud800")
        ] + [
            {"skills": value} for value in (
                None, (), {}, [None], [{}], [{"name": "a"}],
                [{"name": "", "description": "x"}],
                [{"name": "a", "description": "  "}],
                [{"name": 7, "description": "x"}],
                [{"name": "a", "description": []}], [SKILLS[0], SKILLS[0]],
            )
        ] + [
            {"model": value} for value in (None, "", "  ", 7)
        ] + [
            {"api_key": value} for value in (None, "", "  ", "key\nInjected: yes", "kéy", 7)
        ] + [
            {"timeout": value} for value in (None, 0, -1, True, "3", float("nan"), float("inf"), 10 ** 1000)
        ] + [
            {"max_request_bytes": value} for value in (0, -1, True, 1.5, "1000", None)
        ] + [
            {"recent_context": value} for value in (
                {}, "context", ["context"], [{"content": object()}],
                [{"number": float("nan")}], cycles,
                [{1: "non-string key"}], [{"content": ("not", "JSON")}],
            )
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                requester = Mock(return_value=response())
                args = {"query": "video", "skills": SKILLS, "api_key": "fixture-key",
                        "requester": requester, **overrides}
                result = engine.rank_skills(**args)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_input")
                requester.assert_not_called()
        result = engine.rank_skills("video", SKILLS, api_key="fixture-key", requester=False)
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_empty_catalog_still_rejects_invalid_timeout(self):
        result = engine.rank_skills("hello", [], api_key="", timeout=float("nan"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_size_gate_uses_exact_utf8_bytes_without_truncation(self):
        import json
        requester = Mock(return_value=response())
        query = "🎬" * 20
        result = engine.rank_skills(query, SKILLS, api_key="fixture-key", requester=requester)
        self.assertEqual(result["status"], "ok")
        payload = requester.call_args.args[0]
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
        requester.reset_mock()
        result = engine.rank_skills(query, SKILLS, api_key="fixture-key", requester=requester,
                                   max_request_bytes=size - 1)
        self.assertEqual(result["error"]["code"], "request_too_large")
        requester.assert_not_called()
        result = engine.rank_skills(query, SKILLS, api_key="fixture-key", requester=requester,
                                   max_request_bytes=size)
        self.assertEqual(result["status"], "ok")
        requester.assert_called_once()
        self.assertEqual(requester.call_args.args[0], payload)

    def test_failures_are_sanitized_and_never_retried(self):
        from jev_skills.client import EvaluationError
        errors = [
            (RuntimeError("secret-key private request"), "transport_error"),
            (TimeoutError("secret-key private request"), "timeout"),
            (EvaluationError("rate_limited"), "rate_limited"),
        ]
        modified_error = EvaluationError("protocol_error")
        modified_error.args = ("secret-key private request",)
        errors.append((modified_error, "protocol_error"))
        for error, code in errors:
            with self.subTest(code=code):
                requester = Mock(side_effect=error)
                result = engine.rank_skills("private request", SKILLS,
                                           api_key="secret-key", requester=requester)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("scores", result)
                self.assertNotIn("secret-key", str(result))
                self.assertNotIn("private request", str(result))
                requester.assert_called_once()

    def test_default_transport_latency_and_no_cache(self):
        with patch("jev_skills.client.request_evaluation", return_value=response()) as send:
            with patch("jev_skills.engine.perf_counter", side_effect=[100, 100.025]):
                result = engine.rank_skills("video", SKILLS, api_key="fixture-key")
            self.assertAlmostEqual(result["latency_ms"], 25)
            engine.rank_skills("video", SKILLS, api_key="fixture-key")
        self.assertEqual(send.call_count, 2)

    def test_default_engine_and_transport_work_together_offline(self):
        import io
        import json
        import urllib.request
        from email.message import Message
        from urllib.response import addinfourl

        expected = response()
        raw = io.BytesIO(json.dumps(expected).encode("utf-8"))
        wire = addinfourl(raw, Message(), "https://api.typesafe.ai/v1/systemone", 200)
        setattr(wire, "msg", "fixture success")
        with patch.object(urllib.request.HTTPSHandler, "https_open", return_value=wire) as send:
            result = engine.rank_skills("video", SKILLS, api_key="fixture-key")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], expected["model"])
        self.assertEqual(result["usage"], expected["usage"])
        self.assertEqual([item["name"] for item in result["scores"]], ["comfyui", "video", "audio"])
        send.assert_called_once()
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["state"], {"user_request": "video"})
        self.assertTrue(raw.closed)

    def test_extra_catalog_fields_are_not_transmitted(self):
        skill = {**SKILLS[0], "body": "private full body", "path": "/private/path"}
        requester = Mock(return_value={"model": "fixture", "answers": {
            "video": {"type": "noul", "noul": 0.5}}})
        result = engine.rank_skills("video", [skill], api_key="fixture-key", requester=requester)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("private", str(requester.call_args.args[0]))
        self.assertNotIn("body", result["scores"][0])
        self.assertNotIn("path", result["scores"][0])

    def test_build_questions_rejects_bad_catalog_without_rewriting_identifiers(self):
        from jev_skills.client import EvaluationError
        with self.assertRaises(EvaluationError) as caught:
            engine.build_questions([SKILLS[0], SKILLS[0]])
        self.assertEqual(caught.exception.code, "invalid_input")
        questions = engine.build_questions([{"name": "  exact:name  ", "description": "  exact\n"}])
        self.assertEqual(list(questions), ["  exact:name  "])
        self.assertIn("  exact\n", questions["  exact:name  "]["instructions"])


if __name__ == "__main__":
    unittest.main()
