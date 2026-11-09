import unittest
from unittest.mock import MagicMock, patch

from translation_app.auth_config import auth_enabled
from translation_app.services.database import ProjectDatabase
from translation_app.services.translation import _resources


class AuthenticationSwitchTests(unittest.TestCase):
    def test_switch_fails_closed(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertTrue(auth_enabled())
        for value in ('false', 'OFF', '0', 'no'):
            with patch.dict('os.environ', {'LOCATOOL_AUTH_ENABLED': value}):
                self.assertFalse(auth_enabled())
        for value in ('true', 'invalid', ''):
            with patch.dict('os.environ', {'LOCATOOL_AUTH_ENABLED': value}):
                self.assertTrue(auth_enabled())

    def test_database_requires_user_token_unless_explicitly_anonymous(self):
        fake = MagicMock()
        with patch.dict('os.environ', {'SUPABASE_URL': 'https://example.test', 'SUPABASE_KEY': 'public-key'}), \
             patch('supabase.create_client', return_value=fake) as create:
            with self.assertRaises(ValueError):
                ProjectDatabase(None)
            create.assert_not_called()
            ProjectDatabase(None, allow_anonymous=True)
            fake.postgrest.auth.assert_not_called()
            ProjectDatabase('user-token')
            fake.postgrest.auth.assert_called_once_with('user-token')

    def test_worker_refuses_anonymous_context_in_production(self):
        with patch.dict('os.environ', {'LOCATOOL_AUTH_ENABLED': 'true'}):
            with self.assertRaisesRegex(ValueError, '登录已过期'):
                _resources({'direct': False}, {'database_name': 'English'}, {})


if __name__ == '__main__':
    unittest.main()
