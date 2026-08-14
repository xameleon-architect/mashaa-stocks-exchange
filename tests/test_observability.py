import datetime as dt
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync
from observability import ExchangeRun, classify_error, redact_text


class ObservabilityTests(unittest.TestCase):
    def config(self):
        return {
            "store_name": "Склад офиса",
            "log_dir": "logs",
            "output_dir": "output",
            "status_dir": "status",
        }

    def test_daily_logs_are_named_access_and_error(self):
        base = Path("logs")
        access, error = sync.daily_log_paths(base, dt.datetime(2026, 8, 14))
        self.assertEqual(base / "access-2026-08-14.log", access)
        self.assertEqual(base / "error-2026-08-14.log", error)

    def test_failure_emits_full_traceback_to_error_logger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = ExchangeRun(Path(temp_dir), self.config(), mode="dry-run", app_version="test")
            run.start()
            run.begin_step(3, "Тестовая ошибка")
            try:
                raise ValueError("trace-marker")
            except ValueError as exc:
                with self.assertLogs("mashaa.error_trace", level=logging.ERROR) as captured:
                    run.fail(exc)
            text = "\n".join(captured.output)
            self.assertIn("Traceback (most recent call last)", text)
            self.assertIn("ValueError: trace-marker", text)
            self.assertIn(run.run_id, text)
            self.assertIn("Этап 3/8", text)

    def test_completed_run_writes_summary_latest_and_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = ExchangeRun(Path(temp_dir), self.config(), mode="dry-run", app_version="0.5")
            run.start()
            run.begin_step(1, "Проверка")
            run.end_step("OK", "Готово", matched=165)
            payload = run.finish("DRY_RUN_READY", {"changes": 1, "submitted": False, "verified": False})

            latest = json.loads(run.latest_path.read_text(encoding="utf-8"))
            summary = json.loads(run.summary_path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in run.events_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual("DRY_RUN_READY", latest["status"])
            self.assertEqual(payload["run_id"], summary["run_id"])
            self.assertEqual(1, latest["changes"])
            self.assertEqual("exchange_completed", events[-1]["event"])

    def test_finish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = ExchangeRun(Path(temp_dir), self.config(), mode="dry-run", app_version="0.5")
            first = run.finish("NO_CHANGES", {"changes": 0})
            second = run.finish("FAILED", {"changes": 10})
            self.assertEqual(first, second)
            latest = json.loads(run.latest_path.read_text(encoding="utf-8"))
            self.assertEqual("NO_CHANGES", latest["status"])

    def test_error_catalog_marks_validation_as_blocked(self):
        info = classify_error(RuntimeError("Match ratio 50% is below required 98%"))
        self.assertEqual("E_MATCH_RATIO", info.code)
        self.assertTrue(info.blocked)

    def test_secrets_are_redacted(self):
        value = redact_text("Authorization: Bearer secret-token password=very-secret")
        self.assertNotIn("secret-token", value)
        self.assertNotIn("very-secret", value)

    def test_unknown_outcome_is_persisted(self):
        class UnknownError(RuntimeError):
            outcome_unknown = True

        with tempfile.TemporaryDirectory() as temp_dir:
            run = ExchangeRun(Path(temp_dir), self.config(), mode="apply", app_version="0.5")
            run.begin_step(7, "Передача пакета в Tilda")
            payload = run.fail(UnknownError("connection reset"), {"submitted": False})
            self.assertEqual("OUTCOME_UNKNOWN", payload["status"])


if __name__ == "__main__":
    unittest.main()
