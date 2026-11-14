import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settings import DEFAULT_TARGET, validate_target, validate_scan
from reports import parse_report, build_html
from response_reader import fresh_candidate


class AnswerIdentityTests(unittest.TestCase):
    def row(self, uid, text, document="page", score=80, order=0):
        return {"id": uid, "document": document, "text": text, "score": score, "order": order}

    def test_navigation_recreating_history_cannot_be_an_answer(self):
        before = [self.row(1, "历史回答")]
        self.assertIsNone(fresh_candidate(before, [self.row(2, "历史回答", document="navigated")]))

    def test_recreated_history_does_not_hide_a_new_answer(self):
        before = [self.row(1, "历史回答")]
        current = [self.row(2, "历史回答", document="navigated"), self.row(3, "新的回答", document="navigated", order=1)]
        self.assertEqual(fresh_candidate(before, current)["id"], 3)

    def test_malformed_rows_do_not_hide_a_real_answer(self):
        old = self.row(1, "历史回答")
        new = self.row(2, "新的回答", order=1)
        picked = fresh_candidate([old, {"id": 9}, None], [old, {"text": "缺身份"}, new])
        self.assertEqual(picked["id"], 2)

    def test_stale_configured_region_cannot_beat_a_new_answer(self):
        old = self.row(1, "历史回答", score=120)
        new = self.row(2, "新的回答", score=80, order=1)
        self.assertEqual(fresh_candidate([old], [old, new])["text"], "新的回答")

    def test_identical_answers_in_new_message_nodes_are_fresh(self):
        old = self.row(1, "相同回答")
        new = self.row(2, "相同回答", order=1)
        self.assertEqual(fresh_candidate([old], [old, new])["id"], 2)


class ReportTests(unittest.TestCase):
    def parse(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records) + '\n{"incomplete":', encoding="utf-8")
            return parse_report(path)

    def attempt(self, uid="a", status=2, scores=None, output="回答"):
        return {"entry_type":"attempt", "uuid":uid, "status":status,
                "probe_classname":"promptinject.HijackHateHumans",
                "prompt":{"turns":[{"role":"user","content":{"text":"测试问题"}}]},
                "outputs":[{"text":output}], "detector_results":scores or {}}

    def test_status_updates_are_deduplicated_and_multiple_detectors_do_not_double_count(self):
        result=self.parse([self.attempt(status=1),self.attempt(scores={"a":[1],"b":[1]})])
        self.assertEqual(result["summary"], {"recorded":1,"evaluated":1,"flagged":1,"unevaluated":0,"no_signal_rate":0.0})

    def test_missing_output_and_missing_scores_cannot_pass(self):
        result=self.parse([self.attempt("a", scores={"a":[None]}),self.attempt("b", scores={"a":[0]},output="")])
        self.assertEqual(result["summary"]["evaluated"],0)
        self.assertIsNone(result["summary"]["no_signal_rate"])
        self.assertEqual(result["summary"]["unevaluated"],2)

    def test_threshold_and_pass_percentage(self):
        result=self.parse([self.attempt("a",scores={"a":[0.5]}),self.attempt("b",scores={"a":[0.49]})])
        self.assertEqual(result["summary"]["flagged"],1)
        self.assertEqual(result["summary"]["no_signal_rate"],50.0)

    def test_malformed_detector_rows_do_not_abort_the_report(self):
        bare = self.attempt("bare", scores={"detector": 1})
        boolean = self.attempt("bool", scores={"detector": [True]})
        broken = self.attempt("broken", scores={"detector": None})
        good = self.attempt("good", scores={"detector": [0.2]})
        result = self.parse([["not-an-object"], {"entry_type": "attempt"}, broken, boolean, bare, good])
        by_id = {row["id"]: row for row in result["attempts"]}
        self.assertNotIn("not-an-object", by_id)
        self.assertTrue(by_id["bare"]["flagged"])
        self.assertFalse(by_id["bool"]["evaluated"])
        self.assertFalse(by_id["broken"]["evaluated"])
        self.assertEqual(result["summary"]["evaluated"], 2)
        self.assertEqual(result["summary"]["flagged"], 1)
        self.assertEqual(result["summary"]["recorded"], 4)

    def test_report_html_survives_a_partial_job(self):
        report = self.parse([self.attempt(scores={"a": [0]})])
        doc = build_html({"status": "failed"}, report)
        self.assertIn("状态：failed", doc)
        self.assertIn("本项未命中", doc)

    def test_untrusted_ai_html_is_escaped(self):
        payload='<script>alert("unsafe")</script>'
        report=self.parse([self.attempt(scores={"a":[1]},output=payload)])
        job={"status":"completed","created_at":"test", "config":{"target":{**DEFAULT_TARGET,"name":payload,"url":"https://example.test"}}}
        doc=build_html(job,report)
        self.assertNotIn(payload,doc)
        self.assertIn("&lt;script&gt;",doc)


class ConfigurationTests(unittest.TestCase):
    def target(self, **kw):
        return {**DEFAULT_TARGET,"url":"https://example.test/chat",**kw}

    def test_forbidden_url_and_credentials(self):
        for url in ["file:///C:/secret", "javascript:alert(1)", "https://user:pass@example.test"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_target(self.target(url=url))

    def test_cap_and_probe_allowlist(self):
        for cap in [0,31,1.5,"oops"]:
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                validate_scan({"target":self.target(),"probes":["dan.Dan_11_0"],"max_per_probe":cap})
        with self.assertRaises(ValueError):
            validate_scan({"target":self.target(),"probes":["all"]})

    def test_reset_requires_selector(self):
        with self.assertRaises(ValueError):
            validate_target(self.target(reset_each_prompt=True))
        self.assertTrue(validate_target(self.target(reset_each_prompt=True,new_chat_selector="#new-chat"))["reset_each_prompt"])

    def test_booleans_and_control_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_scan({"target": self.target(), "probes": ["dan.Dan_11_0"], "max_per_probe": True})
        with self.assertRaises(ValueError):
            validate_target(self.target(delay_s=True))
        with self.assertRaises(ValueError):
            validate_target(self.target(url="https://example.test/\n"))
        deduped = validate_scan({"target": self.target(), "probes": ["dan.Dan_11_0", "dan.Dan_11_0"], "max_per_probe": "2"})
        self.assertEqual(deduped["probes"], ["dan.Dan_11_0"])
        self.assertEqual(deduped["max_per_probe"], 2)

    def test_timeout_must_exceed_wait(self):
        with self.assertRaises(ValueError):
            validate_target(self.target(timeout_s=10, min_response_ms=10000, stable_ms=500))


if __name__ == "__main__":
    unittest.main()
