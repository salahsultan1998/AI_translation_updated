"""Application factory; web workers and job workers share private storage."""
import os
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, session, url_for

from .auth_config import auth_enabled
from .web.auth import SessionStore, SupabaseAuth, auth

ROOT = Path(__file__).resolve().parent.parent


def create_app(test_config=None):
    load_dotenv(ROOT / '.env')
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
    private_root = Path(os.environ.get('LOCATOOL_STORAGE', ROOT / 'instance')).resolve()
    app.config.update(STORAGE_ROOT=private_root, MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('LOCATOOL_SECURE_COOKIES', '').lower() in {'1', 'true', 'yes'},
                      AUTH_ENABLED=auth_enabled(),
                      SESSION_IDLE_SECONDS=8 * 60 * 60)
    if test_config:
        app.config.update(test_config)
    private_root = Path(app.config['STORAGE_ROOT'])
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not app.config.get('SECRET_KEY'):
        # Stable across process restarts and workers on the same server.
        secret_path = private_root / 'session_secret'
        configured_secret = os.getenv('FLASK_SECRET_KEY')
        if configured_secret:
            app.config['SECRET_KEY'] = configured_secret
        else:
            # Publish the fully written file atomically when multiple workers start.
            with tempfile.NamedTemporaryFile(mode='w', dir=private_root, delete=False) as temp:
                temp.write(secrets.token_hex(32))
                temp_path = Path(temp.name)
            try:
                try:
                    os.link(temp_path, secret_path)
                except FileExistsError:
                    pass
                app.config['SECRET_KEY'] = secret_path.read_text().strip()
            finally:
                temp_path.unlink(missing_ok=True)
        if not app.config['SECRET_KEY']:
            raise RuntimeError('Flask session secret is empty')
    app.extensions['auth_provider'] = app.config.get('AUTH_PROVIDER') or SupabaseAuth(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))
    app.extensions['sessions'] = SessionStore(private_root, app.config['SESSION_IDLE_SECONDS'])
    from .jobs import JobStore
    app.extensions['jobs'] = app.config.get('JOB_STORE') or JobStore(private_root / 'jobs')

    @app.before_request
    def security():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            if not secrets.compare_digest(str(supplied), str(session.get('csrf', ''))):
                return jsonify(error='请求验证失败，请刷新页面重试。'), 403
        if app.config['AUTH_ENABLED']:
            g.user = app.extensions['sessions'].get(session.get('sid'), app.extensions['auth_provider'].refresh)
        else:
            # Keep separate browser sessions' development jobs apart.
            user_id = session.setdefault('development_user_id', secrets.token_urlsafe(24))
            g.user = {'user_id': 'development-' + user_id, 'email': '', 'access_token': None}
        if request.endpoint and request.endpoint not in {'auth.login', 'auth.login_page', 'static'} and not g.user:
            if request.path.startswith('/api/'):
                return jsonify(error='登录已过期，请重新登录。'), 401
            return redirect(url_for('auth.login_page'))

    @app.context_processor
    def globals_for_templates():
        return {'csrf_token': session.get('csrf', ''), 'current_user_email': g.user['email'] if getattr(g, 'user', None) else None,
                'auth_enabled': app.config['AUTH_ENABLED']}

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error='文件过大，请上传不超过 25 MB 的文件。'), 413

    from .web.pages import pages
    from .web.api import api
    app.register_blueprint(auth)
    app.register_blueprint(pages)
    app.register_blueprint(api)
    return app
