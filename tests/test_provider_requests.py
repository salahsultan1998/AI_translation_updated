import unittest
from unittest.mock import MagicMock, patch

import requests

from translation_app.services.translation import _call_model


class ProviderRequestTests(unittest.TestCase):
    def test_translation_request_disables_reasoning_and_preserves_json_format(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', return_value=self.response(200)) as post:
            result = self.call()
        data = post.call_args.kwargs['json']
        self.assertEqual(data['reasoning'], {'enabled': False})
        self.assertEqual(data['response_format'], {'type': 'json_object'})
        self.assertEqual(result, [{'row_id': 0, 'translated_text': 'test'}])

    def test_prompt_logs_show_input_lengths_and_optional_full_content_without_api_key(self):
        for enabled in ('true', 'false'):
            with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'secret-test-key',
                                          'TRANSLATION_LOG_PROMPTS': enabled}), \
                 patch('translation_app.services.translation.provider_runtime.post', return_value=self.response(200)), \
                 self.assertLogs('translation_app.services.translation', level='INFO') as captured:
                self.call()
            logs = '\n'.join(captured.output)
            self.assertIn('system_chars=', logs)
            self.assertIn('user_chars=', logs)
            self.assertIn('estimated_input_tokens=', logs)
            self.assertNotIn('secret-test-key', logs)
            self.assertEqual('role=system BEGIN' in logs, enabled == 'true')
            self.assertEqual('测试' in logs, enabled == 'true')

    def call(self):
        return _call_model([(0, '测试', '')], 'Translate.', 'English', '', {'model': 'primary'})

    @staticmethod
    def response(status):
        response = MagicMock()
        response.status_code = status
        response.headers = {}
        response.json.return_value = {'choices': [{'message': {'content': '{"translations": [{"row_id": 0, "translated_text": "test"}]}'}}]}
        return response

    def test_permanent_4xx_fails_immediately(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', return_value=self.response(401)) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                self.call()
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_transient_statuses_retry_and_use_fallback(self):
        seen_models = []
        seen_reasoning = []
        responses = iter([self.response(429), self.response(503), self.response(200)])
        def post(*args, **kwargs):
            seen_models.append(kwargs['json']['model'])
            seen_reasoning.append(dict(kwargs['json']['reasoning']))
            return next(responses)
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key', 'OPENROUTER_FALLBACK_MODEL': 'fallback'}), \
             patch('translation_app.services.translation.provider_runtime.post', side_effect=post), \
             patch('translation_app.services.translation.time.sleep') as sleep:
            result = self.call()
        self.assertEqual(result[0]['translated_text'], 'test')
        self.assertEqual(seen_models, ['primary', 'primary', 'fallback'])
        self.assertEqual(seen_reasoning, [{'enabled': False}] * 3)
        self.assertEqual(sleep.call_count, 2)

    def test_read_timeout_does_not_replay_a_potentially_billable_request(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', side_effect=requests.ReadTimeout()) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '响应超时'):
                self.call()
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_connection_timeout_has_a_bounded_number_of_retries(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', side_effect=requests.ConnectTimeout()) as post, \
             patch('translation_app.services.translation.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '无法连接'):
                self.call()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_invalid_json_is_a_format_error_and_is_not_replayed(self):
        response = self.response(200)
        response.json.side_effect = requests.exceptions.JSONDecodeError('bad', '', 0)
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', return_value=response) as post:
            with self.assertRaisesRegex(ValueError, '格式无效'):
                self.call()
        self.assertEqual(post.call_count, 1)

    def test_malformed_usage_does_not_fail_valid_translations(self):
        for usage in ([], 'wrong', {'prompt_tokens': 'not a number', 'completion_tokens': -1}):
            response = self.response(200)
            body = response.json.return_value
            body['usage'] = usage
            records = []
            with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
                 patch('translation_app.services.translation.provider_runtime.post', return_value=response):
                result = _call_model([(0, '测试', '')], 'Translate', 'English', '',
                                     {'model': 'primary', '_record_request': records.append})
            self.assertEqual(result[0]['translated_text'], 'test')
            self.assertEqual(records[0]['usage'], {})

    def test_rate_limit_retry_after_and_actual_usage_metrics(self):
        limited, ok = self.response(429), self.response(200)
        limited.headers['Retry-After'] = '5'
        ok.json.return_value['usage'] = {'prompt_tokens': 42, 'completion_tokens': 12}
        records = []
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('translation_app.services.translation.provider_runtime.post', side_effect=[limited, ok]), \
             patch('translation_app.services.translation.time.sleep') as sleep:
            _call_model([(0, '测试', '')], 'Translate', 'English', '',
                        {'model': 'primary', '_record_request': records.append})
        sleep.assert_called_once_with(5)
        self.assertEqual([item['status'] for item in records], [429, 200])
        self.assertEqual(records[-1]['usage']['completion_tokens'], 12)


if __name__ == '__main__':
    unittest.main()
