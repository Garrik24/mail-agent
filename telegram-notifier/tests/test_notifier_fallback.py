import os, sys, types, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic  # noqa: E402
import httpx  # noqa: E402
import gemini_fallback  # noqa: E402
import notifier  # noqa: E402

EMAIL = {"sender_name": "Иван", "sender_email": "i@x.ru", "subject": "Срочно: КП", "date": "03.10.2026", "body": "Нужно КП до пятницы"}
GOOD = '{"summary":"Просят КП","action":"Подготовить КП","urgency":"высокая"}'


def api_error(status, message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"type": "error", "error": {"type": "x", "message": message}})
    return anthropic.APIStatusError(message, response=resp, body=None)


def claude(create):
    return mock.patch.object(anthropic, "Anthropic", return_value=mock.Mock(messages=mock.Mock(create=create)))


class NotifierFallback(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(notifier, "ANTHROPIC_API_KEY", "k"); p.start(); self.addCleanup(p.stop)

    def test_skips_thinking_block(self):
        msg = types.SimpleNamespace(content=[types.SimpleNamespace(type="thinking"), types.SimpleNamespace(type="text", text=GOOD)])
        with claude(mock.Mock(return_value=msg)), mock.patch.object(gemini_fallback, "generate") as g:
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["urgency"], "высокая")
        g.assert_not_called()

    def test_blocked_org_uses_gemini(self):
        with claude(mock.Mock(side_effect=api_error(400, "This organization has been disabled."))), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", return_value=GOOD) as g:
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["action"], "Подготовить КП")
        self.assertTrue(g.call_args.kwargs["json_mode"])

    def test_request_error_does_not_use_gemini(self):
        with claude(mock.Mock(side_effect=api_error(400, "max_tokens: bad"))), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate") as g:
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["action"], "Проверить письмо вручную")
        g.assert_not_called()

    def test_no_claude_key_uses_gemini(self):
        with mock.patch.object(notifier, "ANTHROPIC_API_KEY", ""), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", return_value="```json\n" + GOOD + "\n```"):
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["summary"], "Просят КП")

    def test_no_keys_at_all_default(self):
        with mock.patch.object(notifier, "ANTHROPIC_API_KEY", ""), mock.patch.object(gemini_fallback, "enabled", return_value=False):
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["action"], "Проверить письмо")

    def test_both_down_default(self):
        with claude(mock.Mock(side_effect=api_error(429, "rate"))), \
             mock.patch.object(gemini_fallback, "enabled", return_value=True), \
             mock.patch.object(gemini_fallback, "generate", side_effect=RuntimeError("boom")):
            self.assertEqual(notifier.analyze_with_claude(EMAIL)["action"], "Проверить письмо вручную")


if __name__ == "__main__":
    unittest.main()
