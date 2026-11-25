"""Synchronous translation responses for the development platform."""
import asyncio
import hmac
import logging
import os
from pathlib import Path
import threading
import time
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from supabase_auth.errors import AuthApiError

from .auth_config import auth_enabled
from .services import translation
from .services.database import ProjectDatabase
from .services.knowledge import KnowledgeError, KnowledgeStore
from .services.projects import list_languages, list_projects, validate_selection
from .services.prompt_editor import PromptUnavailable, prompt_settings, read_prompts
from .services.settings import DEFAULT_BATCH_SIZE, translation_settings
from .services.supabase_auth import SupabaseAuth
from .services.translation_state import check_cancelled

load_dotenv(Path(__file__).resolve().parents[1] / '.env')
docs_enabled = os.getenv('LOCATOOL_API_DOCS', '').strip().lower() == 'true'
app = FastAPI(openapi_url='/openapi.json' if docs_enabled else None)
TIMEOUT_SECONDS = 120
_service_session = None
_service_lock = threading.Lock()
_logger = logging.getLogger(__name__)


class TextRow(BaseModel):
    source_text: str
    id: str | None = None
    context: str | None = None


class GlossaryRequest(BaseModel):
    game_id: str
    target_language: str


class TranslateRequest(GlossaryRequest):
    texts: list[TextRow] = Field(min_length=1)
    model: str | None = None
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE, ge=1, le=50)
    use_smart_budget: bool = True
    custom_instructions: str = Field(default='', max_length=20000)
    validation_enabled: Literal[True] = Field(default=True, alias='validate')
    use_rag: Literal[True] = True


def _access_token(api_key, authorization, database_enabled, options):
    configured = os.getenv('LOCATOOL_API_KEY', '').strip()
    scheme, _, bearer = (authorization or '').partition(' ')
    bearer = bearer.strip() if scheme.lower() == 'bearer' else ''
    key_matches = configured and any(
        hmac.compare_digest(configured.encode(), value.encode())
        for value in ((api_key or '').strip(), bearer)
    )
    auth = SupabaseAuth(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))
    if key_matches:
        if not database_enabled:
            return None
        email = os.getenv('LOCATOOL_API_SUPABASE_EMAIL')
        password = os.getenv('LOCATOOL_API_SUPABASE_PASSWORD')
        if not email or not password:
            raise HTTPException(503, 'API 的 Supabase 读取账号尚未配置')
        global _service_session
        with _service_lock:
            check_cancelled(options)
            if not _service_session or _service_session['expires_at'] <= time.time() + TIMEOUT_SECONDS:
                try:
                    _service_session = auth.login(email, password)
                except (AuthApiError, ValueError, RuntimeError):
                    raise HTTPException(503, 'API 的 Supabase 读取账号登录失败') from None
            return _service_session['access_token']
    if bearer and database_enabled:
        try:
            if auth.client().auth.get_user(bearer).user:
                return bearer
        except AuthApiError as error:
            if error.status not in {400, 401, 403}:
                raise
    raise HTTPException(401, '请提供有效的 API Key 或 Supabase Bearer Token')


def _selection(game_id, language):
    game = game_id.strip().casefold()
    if not game:
        raise ValueError('项目不存在或未启用')
    project = next((item for item in list_projects() if item['enabled'] and game in
                    {item['id'].casefold(), (item.get('database_id') or '').strip().casefold()}), None)
    if not project:
        raise ValueError('项目不存在或未启用')
    language = language.strip().casefold()
    for info in list_languages(project['id']):
        if language in {info['id'].casefold(), info['database_name'].strip().casefold()}:
            return project['id'], info['id']
    raise ValueError('该项目不支持此语言')


def _translate(request, api_key, authorization, cancelled):
    started = time.perf_counter()
    enabled = auth_enabled()
    settings = {'_cancel_event': cancelled}
    token = _access_token(api_key, authorization, enabled, settings)
    try:
        project, language = _selection(request.game_id, request.target_language)
        settings.update(translation_settings(request.model_dump(exclude={'texts'})))
        rows = [(index, item.source_text.strip(), (item.context or '').strip())
                for index, item in enumerate(request.texts) if item.source_text.strip()]
        if not rows:
            raise ValueError('没有可翻译的源文本')
    except ValueError as error:
        raise HTTPException(400, str(error)) from None

    check_cancelled(settings)
    database = ProjectDatabase(token) if enabled else None
    settings.update(prompt_settings(read_prompts(project, language, database)), access_token=token)
    translated = translation.translate_rows(rows, project, language, settings)
    items = []
    for index, source, _ in rows:
        item = request.texts[index]
        items.append({'id': item.id if item.id is not None else str(index), 'source_text': source,
                      'translated_text': translated[index], 'context': item.context, 'issues': []})
    return {'game_id': request.game_id, 'target_language': request.target_language, 'model': settings['model'],
            'count': len(items), 'elapsed_ms': int((time.perf_counter() - started) * 1000),
            'translations': items, 'is_valid': True, 'issues': []}


def _knowledge_export(request, api_key, authorization, cancelled, kind):
    settings = {'_cancel_event': cancelled}
    token = _access_token(api_key, authorization, auth_enabled(), settings)
    if not token:
        raise HTTPException(403, '知识库导出需要启用 Supabase 登录权限')
    try:
        project_id, language_id = _selection(request.game_id, request.target_language)
        project, language = validate_selection(project_id, language_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    if project['direct']:
        raise HTTPException(400, '直接翻译没有项目知识库')
    check_cancelled(settings)
    store = KnowledgeStore(ProjectDatabase(token), project['database_id'].strip().lower(),
                           language['database_name'].strip().lower(), kind)
    store.require_access()
    fields = ('source', 'target', 'scene', 'note')
    key = 'terms'
    if kind == 'memory':
        fields += ('string_key',)
        key = 'history'
    entries = [{field: row[field] for field in fields}
               for row in store.snapshot_rows(active_only=True, settings=settings)]
    return {'game_id': project_id, 'target_language': language_id, 'count': len(entries), key: entries}


async def _export_knowledge(request, x_api_key, authorization, kind):
    cancelled = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_knowledge_export, request, x_api_key, authorization, cancelled, kind), TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, '知识库读取超时') from None
    except HTTPException:
        raise
    except KnowledgeError as error:
        raise HTTPException(error.status, str(error)) from None
    except Exception as error:
        _logger.warning('Knowledge export API failed: %s', type(error).__name__)
        raise HTTPException(502, '知识库读取失败，请稍后重试') from None
    finally:
        cancelled.set()


@app.post('/v1/glossary')
async def glossary(request: GlossaryRequest, x_api_key: str | None = Header(default=None),
                   authorization: str | None = Header(default=None)):
    return await _export_knowledge(request, x_api_key, authorization, 'terms')


@app.post('/v1/history')
async def history(request: GlossaryRequest, x_api_key: str | None = Header(default=None),
                  authorization: str | None = Header(default=None)):
    return await _export_knowledge(request, x_api_key, authorization, 'memory')


@app.post('/v1/translate')
async def translate(request: TranslateRequest, x_api_key: str | None = Header(default=None),
                    authorization: str | None = Header(default=None)):
    cancelled = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_translate, request, x_api_key, authorization, cancelled), TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, '翻译请求超时') from None
    except HTTPException:
        raise
    except PromptUnavailable as error:
        raise HTTPException(503, str(error)) from None
    except KnowledgeError as error:
        raise HTTPException(error.status, str(error)) from None
    except Exception as error:
        _logger.warning('Translation API failed: %s', type(error).__name__)
        raise HTTPException(502, '翻译失败，请检查输入或稍后重试') from None
    finally:
        cancelled.set()
