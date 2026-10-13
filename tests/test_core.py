import json
import sys
import uuid
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
        directory = Path(__file__).resolve().parents[1] / ".runtime" / "unit-tests"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (str(uuid.uuid4()) + ".jsonl")
        try:
            path.write_text("\n".join(json.dumps(r) for r in records) + '\n{"incomplete":', encoding="utf-8")
            return parse_report(path)
        finally:
            path.unlink(missing_ok=True)

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


if __name__ == "__main__":
    unittest.main()
