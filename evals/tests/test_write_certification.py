import os
import tempfile
import unittest
from pathlib import Path

import yaml

from ..run_eval import EvalResult, write_certification


class WriteCertificationTests(unittest.TestCase):
    def test_writes_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "certified_models.yaml"
            result = EvalResult(
                model_id="us.amazon.nova-micro-v1:0",
                eval_pass_rate=0.95,
                safety_score=1.0,
                p95_latency_ms=1200.0,
                eval_avg_cost_per_request_usd=0.001,
            )

            write_certification(result, certified_models_path=str(path))

            data = yaml.safe_load(path.read_text())
            self.assertIn("us.amazon.nova-micro-v1:0", data["certified_models"])
            self.assertEqual(data["certified_models"]["us.amazon.nova-micro-v1:0"]["eval_pass_rate"], 0.95)

    def test_merges_into_existing_file_without_dropping_other_models(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "certified_models.yaml"
            path.write_text(
                yaml.safe_dump({"certified_models": {"other-model": {"eval_pass_rate": 0.9}}})
            )
            result = EvalResult(
                model_id="us.amazon.nova-micro-v1:0",
                eval_pass_rate=0.95,
                safety_score=1.0,
                p95_latency_ms=1200.0,
                eval_avg_cost_per_request_usd=0.001,
            )

            write_certification(result, certified_models_path=str(path))

            data = yaml.safe_load(path.read_text())
            self.assertIn("other-model", data["certified_models"])
            self.assertIn("us.amazon.nova-micro-v1:0", data["certified_models"])

    def test_no_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "certified_models.yaml"
            result = EvalResult(
                model_id="us.amazon.nova-micro-v1:0",
                eval_pass_rate=0.95,
                safety_score=1.0,
                p95_latency_ms=1200.0,
                eval_avg_cost_per_request_usd=0.001,
            )

            write_certification(result, certified_models_path=str(path))

            self.assertEqual(os.listdir(d), ["certified_models.yaml"])


if __name__ == "__main__":
    unittest.main()
