import unittest
from unittest.mock import MagicMock, patch

import requests

from translation_app.services.translation import _call_model


class ProviderRequestTests(unittest.TestCase):
    def call(self):
        return _call_model([(0, '测试', '')], 'Translate.', 'English', '', {'model': 'primary'})

    @staticmethod
    def response(status):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = {'choices': [{'message': {'content': '{"translations": [{"row_id": 0, "translated_text": "test"}]}'}}]}
        return response

    def test_permanent_4xx_fails_immediately(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.requests.post', return_value=self.response(401)) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                self.call()
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_transient_statuses_retry_and_use_fallback(self):
        seen_models = []
        responses = iter([self.response(429), self.response(503), self.response(200)])
        def post(*args, **kwargs):
            seen_models.append(kwargs['json']['model'])
            return next(responses)
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key', 'OPENROUTER_FALLBACK_MODEL': 'fallback'}), \
             patch('translation_app.services.translation.requests.post', side_effect=post), \
             patch('translation_app.services.translation.time.sleep') as sleep:
            result = self.call()
        self.assertEqual(result[0]['translated_text'], 'test')
        self.assertEqual(seen_models, ['primary', 'primary', 'fallback'])
        self.assertEqual(sleep.call_count, 2)

    def test_read_timeout_does_not_replay_a_potentially_billable_request(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.requests.post', side_effect=requests.ReadTimeout()) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '响应超时'):
                self.call()
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_connection_timeout_has_a_bounded_number_of_retries(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.requests.post', side_effect=requests.ConnectTimeout()) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '无法连接'):
                self.call()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
