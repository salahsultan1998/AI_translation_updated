"""Supabase login with opaque, server-side sessions."""
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for


auth = Blueprint("auth", __name__)


class SessionStore:
    def __init__(self, root, idle_seconds=28800):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "sessions.sqlite3"
        self.idle_seconds = idle_seconds
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, email TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, expires_at REAL NOT NULL, last_active REAL NOT NULL)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, user_id, email, access_token, refresh_token, expires_at):
        key = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (key, user_id, email, access_token, refresh_token, expires_at, time.time()))
        return key

    def get(self, key, refresh):
        if not key:
            return None
        now = time.time()
        with self.connect() as db:
            # Serialize refreshes across web processes because refresh tokens rotate.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (key,)).fetchone()
            if not row:
                return None
            if now - row["last_active"] > self.idle_seconds:
                db.execute("DELETE FROM sessions WHERE id=?", (key,))
                return None
            result = dict(row)
            if result["expires_at"] <= now + 60:
                try:
                    token = refresh(result["refresh_token"])
                except Exception:
                    db.execute("DELETE FROM sessions WHERE id=?", (key,))
                    return None
                result.update(access_token=token["access_token"], refresh_token=token["refresh_token"], expires_at=token["expires_at"])
                db.execute("UPDATE sessions SET access_token=?, refresh_token=?, expires_at=? WHERE id=?", (result["access_token"], result["refresh_token"], result["expires_at"], key))
            db.execute("UPDATE sessions SET last_active=? WHERE id=?", (now, key))
            return result

    def delete(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (key,))


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


@auth.get('/login')
def login_page():
    if getattr(g, 'user', None):
        return redirect(url_for('pages.projects'))
    return render_template('login.html', error=None)


@auth.post('/login')
def login():
    if not current_app.config['AUTH_ENABLED']:
        return redirect(url_for('pages.projects'))
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    try:
        if not email or not password:
            raise ValueError('请填写邮箱和密码')
        data = current_app.extensions['auth_provider'].login(email, password)
        current_app.extensions['sessions'].delete(session.get('sid', ''))
        session.clear()
        session['sid'] = current_app.extensions['sessions'].create(**data)
        return redirect(url_for('pages.projects'))
    except Exception:
        return render_template('login.html', error='登录失败，请检查邮箱、密码或服务配置。'), 401


@auth.post('/logout')
def logout():
    current_app.extensions['sessions'].delete(session.get('sid', ''))
    session.clear()
    return redirect(url_for('auth.login_page' if current_app.config['AUTH_ENABLED'] else 'pages.projects'))
