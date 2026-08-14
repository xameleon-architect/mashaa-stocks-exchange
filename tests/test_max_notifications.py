import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import max_notifications


class FakeResponse:
    def __init__(self, payload: bytes = b'{"message": {}}'):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class MaxNotificationTests(unittest.TestCase):
    def payload(self, status="SUBMITTED_UNVERIFIED"):
        return {
            "app_version": "0.6",
            "run_id": "RUN-123",
            "status": status,
            "mode": "apply",
            "store": "Склад офиса",
            "final_stage_index": 8,
            "final_stage": "Проверка результата",
            "matched": 165,
            "stock_rows": 165,
            "changes": 12,
            "duration_seconds": 17.4,
        }

    def config(self):
        return {
            "max_notifications": {
                "api_base": "https://platform-api2.max.ru",
                "timeout_seconds": 10,
                "notify": True,
                "statuses": ["SUBMITTED_UNVERIFIED", "FAILED"],
            }
        }

    def test_disabled_notification_does_not_make_request(self):
        with mock.patch.dict(os.environ, {"MAX_NOTIFICATIONS_ENABLED": "false"}, clear=True), \
                mock.patch("urllib.request.urlopen") as urlopen:
            outcome = max_notifications.notify_exchange(self.payload(), self.config())
        self.assertEqual("skipped", outcome["status"])
        self.assertFalse(outcome["enabled"])
        urlopen.assert_not_called()

    def test_non_selected_status_does_not_make_request(self):
        env = {
            "MAX_NOTIFICATIONS_ENABLED": "true",
            "MAX_BOT_TOKEN": "secret-token",
            "MAX_CHAT_ID": "123456789",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("urllib.request.urlopen") as urlopen:
            outcome = max_notifications.notify_exchange(self.payload("NO_CHANGES"), self.config())
        self.assertEqual("status_not_selected", outcome["reason"])
        urlopen.assert_not_called()

    def test_request_targets_group_chat_and_uses_authorization_header(self):
        env = {
            "MAX_NOTIFICATIONS_ENABLED": "true",
            "MAX_BOT_TOKEN": "secret-token",
            "MAX_CHAT_ID": "123456789",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            outcome = max_notifications.notify_exchange(self.payload(), self.config())

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("sent", outcome["status"])
        self.assertIn("chat_id=123456789", request.full_url)
        self.assertNotIn("user_id=", request.full_url)
        self.assertEqual("secret-token", request.get_header("Authorization"))
        self.assertTrue(body["notify"])
        self.assertIn("Mashaa Tilda Sync v0.6", body["text"])
        self.assertNotIn("secret-token", body["text"])

    def test_http_failure_is_reported_without_raising(self):
        env = {
            "MAX_NOTIFICATIONS_ENABLED": "true",
            "MAX_BOT_TOKEN": "secret-token",
            "MAX_CHAT_ID": "123456789",
        }
        error = urllib.error.HTTPError(
            "https://platform-api2.max.ru/messages?chat_id=123456789",
            403,
            "Forbidden",
            {},
            None,
        )
        error.read = mock.Mock(return_value=b"forbidden")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            outcome = max_notifications.notify_exchange(self.payload(), self.config())
        self.assertEqual("failed", outcome["status"])
        self.assertEqual("***6789", outcome["chat_id_masked"])
        self.assertNotIn("secret-token", json.dumps(outcome))

    def test_error_message_has_action_but_no_traceback(self):
        payload = self.payload("FAILED")
        payload["error"] = {
            "code": "E_CML_IMPORT",
            "message": "Импорт не завершён",
            "action": "Проверить историю Tilda",
        }
        message = max_notifications.format_exchange_message(payload)
        self.assertIn("E_CML_IMPORT", message)
        self.assertIn("Проверить историю Tilda", message)
        self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
