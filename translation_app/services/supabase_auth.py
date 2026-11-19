"""Supabase authentication shared by the web app and the translation API."""
import time


class SupabaseAuth:
    def __init__(self, url, key):
        self.url, self.key = url, key

    def client(self):
        if not self.url or not self.key:
            raise RuntimeError("Supabase 尚未配置")
        from supabase import create_client
        return create_client(self.url, self.key)

    @staticmethod
    def token_data(auth_session):
        return {"access_token": auth_session.access_token, "refresh_token": auth_session.refresh_token,
                "expires_at": auth_session.expires_at or (time.time() + auth_session.expires_in)}

    def login(self, email, password):
        response = self.client().auth.sign_in_with_password({"email": email, "password": password})
        if not response.user or not response.session:
            raise ValueError("登录失败")
        return {"user_id": str(response.user.id), "email": response.user.email or email, **self.token_data(response.session)}

    def refresh(self, refresh_token):
        response = self.client().auth.refresh_session(refresh_token)
        return self.token_data(response.session)

    def data_client(self, access_token):
        client = self.client()
        client.postgrest.auth(access_token)
        return client
