"""Authenticated endpoints used by the Flask workspace."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import zipfile

from flask import Blueprint, current_app, g, jsonify, request, send_file, url_for
from werkzeug.utils import secure_filename

from translation_app.services.projects import get_project, validate_selection
from translation_app.services.spreadsheets import has_mapped_rows, inspect_file

api = Blueprint('api', __name__, url_prefix='/api')


@api.errorhandler(ValueError)
def bad_input(error):
    return jsonify(error=str(error)), 400


@api.errorhandler(Exception)
def failed(error):
    current_app.logger.warning('Request failed: %s', type(error).__name__)
    return jsonify(error='操作失败，请检查输入或稍后重试。'), 500


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('请求格式无效')
    return data


def owner():
    return g.user['user_id']


def engine():
    from translation_app.services.database import ProjectDatabase
    return ProjectDatabase(g.user['access_token'], allow_anonymous=not current_app.config['AUTH_ENABLED'],
                           storage_root=current_app.config['STORAGE_ROOT'])


def scoped_project(project_id, language=None):
    project = get_project(project_id)
    if project['direct']:
        raise ValueError('直接翻译不使用项目知识库')
    if language is None:
        return project
    project, lang = validate_selection(project_id, language)
    return project, lang['database_name']


@api.post('/projects/<project_id>/knowledge/preload')
def preload_knowledge(project_id):
    project, language = validate_selection(project_id, payload().get('language'))
    if project['direct']:
        raise ValueError('直接翻译不使用项目知识库')
    from translation_app.services.resources import preload_resources
    settings = {'access_token': g.user['access_token'],
                '_limiter_root': str(current_app.extensions['jobs'].root / 'provider-slots')}
    return jsonify(preload_resources(project, language, settings))


def upload_root():
    key = hashlib.sha256(owner().encode()).hexdigest()
    path = Path(current_app.config['STORAGE_ROOT']) / 'uploads' / key
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def upload_path(upload_id):
    if not isinstance(upload_id, str) or not re.fullmatch(r'[0-9a-f]{32}', upload_id):
        raise ValueError('上传文件不存在，请重新上传')
    folder = upload_root() / upload_id
    try:
        meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        if time.time() - meta['created'] > 24 * 3600:
            raise ValueError('上传文件已过期，请重新上传')
        path = folder / ('input' + meta['ext'])
        if not path.is_file():
            raise ValueError('上传文件不存在，请重新上传')
        return path, meta
    except (OSError, KeyError, json.JSONDecodeError):
        raise ValueError('上传文件不存在，请重新上传')


def read_upload(file):
    if not file or not file.filename:
        raise ValueError('请选择文件')
    name = Path(file.filename.replace('\\', '/')).name
    ext = Path(name).suffix.lower()
    if ext not in {'.csv', '.xlsx'}:
        raise ValueError('仅支持 CSV 和 XLSX 文件')
    raw = file.read()
    if not raw:
        raise ValueError('文件为空')
    if ext == '.xlsx':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 200 * 1024 * 1024:
                    raise ValueError('文件解压后过大，请拆分后重试')
        except zipfile.BadZipFile:
            raise ValueError('Excel 文件格式无效')
    return name, ext, raw


@api.post('/uploads/preview')
def preview_upload():
    name, ext, raw = read_upload(request.files.get('file'))
    upload_id = uuid.uuid4().hex
    folder = upload_root() / upload_id
    folder.mkdir(mode=0o700)
    try:
        path = folder / ('input' + ext)
        path.write_bytes(raw)
        path.chmod(0o600)
        result = inspect_file(path)
        (folder / 'meta.json').write_text(json.dumps({'filename': name, 'ext': ext, 'created': time.time()}), encoding='utf-8')
        for sheet in result['sheets']:
            sheet['rows'] = sheet.pop('preview')
        return jsonify(upload_id=upload_id, filename=name, **result)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@api.post('/jobs')
def create_job():
    data = payload()
    project_id, language = data.get('project_id'), data.get('language')
    project, _ = validate_selection(project_id, language)
    from translation_app.services.settings import model_settings
    options, default = model_settings()
    model = data.get('model') or default
    if model not in options:
        raise ValueError('模型配置无效')
    size = data.get('batch_size', 10)
    if type(size) is not int or not 1 <= size <= 50:
        raise ValueError('每批行数必须在 1 到 50 之间')
    if data.get('custom_instructions'):
        raise ValueError('请先在提示词配置中保存 Instructions，再提交翻译')
    if 'manual_rows' in data:
        manual = data['manual_rows']
        if not isinstance(manual, list) or not 1 <= len(manual) <= 10000:
            raise ValueError('请输入 1 至 10000 行文本')
        target = data.get('manual_target') or language
        if not isinstance(target, str) or not target.strip() or target.strip() in {'源文本', '上下文'}:
            raise ValueError('目标列名称无效')
        target = target.strip()
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['源文本', '上下文', target])
        for row in manual:
            if (not isinstance(row, dict) or not isinstance(row.get('source_text', ''), str)
                    or not isinstance(row.get('context', ''), str)
                    or not isinstance(row.get('translated_text', ''), str)):
                raise ValueError('手动输入内容无效')
            writer.writerow([row.get('source_text', ''), row.get('context', ''), row.get('translated_text', '')])
        if not any(row.get('source_text', '').strip() for row in manual):
            raise ValueError('请至少输入一行原文')
        raw = out.getvalue().encode('utf-8-sig')
        filename = 'manual_input.csv'
        mappings = {'CSV_Sheet': {'source': '源文本', 'context': '上下文', 'target': target, '_manual_entry': True}}
    else:
        path, meta = upload_path(data.get('upload_id'))
        filename, raw = meta['filename'], path.read_bytes()
        sheets = data.get('sheets')
        if not isinstance(sheets, list) or not sheets:
            raise ValueError('请选择工作表并设置列映射')
        mappings = {}
        for sheet in sheets:
            if not isinstance(sheet, dict) or not isinstance(sheet.get('name'), str) or sheet['name'] in mappings:
                raise ValueError('工作表映射无效')
            mappings[sheet['name']] = {k: sheet.get(k) for k in ('source', 'context', 'target')}
        # Preview fully checked this private upload; avoid materializing it again.
        if not has_mapped_rows(path, mappings):
            raise ValueError('未找到需要翻译的文本')
    if len(raw) > current_app.config['MAX_CONTENT_LENGTH']:
        raise ValueError('输入内容过大')
    settings = {'model': model, 'batch_size': size, 'use_smart_budget': bool(data.get('use_smart_budget', True)),
                'use_exact_memory': data.get('use_exact_memory') is True}
    from translation_app.services.projects import load_prompt_pair
    pair = load_prompt_pair(project_id, language)
    settings.update(system_prompt=pair['system'], instructions=pair['instructions'])
    if not project['direct']:
        settings['access_token'] = g.user['access_token']
    store = current_app.extensions['jobs']
    if sum(item['status'] in {'queued', 'running'} for item in store.list(owner(), limit=100)) >= 3:
        raise ValueError('同时最多提交三个任务，请等待已有任务完成')
    job = store.create(owner(), project_id, language, filename, raw, mappings, settings)
    return jsonify(job), 202


@api.route('/projects/<project_id>/prompts', methods=['GET', 'POST'])
def prompts(project_id):
    from translation_app.services.prompt_editor import PromptConflict, read_prompts, save_prompt
    if request.method == 'GET':
        return jsonify(prompts=read_prompts(project_id, request.args.get('language')))
    data = payload()
    try:
        saved = save_prompt(project_id, data.get('language'), data.get('kind'), data.get('content'), data.get('version'))
    except PromptConflict:
        return jsonify(error='提示词已被其他人修改，请重新加载后再保存。'), 409
    return jsonify(prompt=saved)


@api.get('/jobs')
def job_list():
    project = request.args.get('project_id')
    if project:
        get_project(project)
    return jsonify(jobs=current_app.extensions['jobs'].list(owner(), project))


@api.post('/jobs/clear')
def clear_jobs():
    project = payload().get('project_id')
    get_project(project)
    current_app.extensions['jobs'].clear_project(owner(), project)
    # Uploads are temporary to the current browser/user, not project data.
    shutil.rmtree(upload_root(), ignore_errors=True)
    return jsonify(ok=True)


@api.get('/jobs/<job_id>')
def job_status(job_id):
    job = current_app.extensions['jobs'].get(owner(), job_id)
    if not job:
        return jsonify(error='任务不存在'), 404
    job['download_url'] = url_for('api.job_download', job_id=job_id) if job['status'] == 'completed' else None
    return jsonify(job)


@api.post('/jobs/<job_id>/retry')
def retry_job(job_id):
    store = current_app.extensions['jobs']
    job = store.get(owner(), job_id)
    if not job:
        return jsonify(error='任务不存在'), 404
    validate_selection(job['project_id'], job['language'])
    return jsonify(store.retry(owner(), job_id, g.user['access_token'])), 202


@api.get('/jobs/<job_id>/download')
def job_download(job_id):
    store = current_app.extensions['jobs']
    path = store.result_path(owner(), job_id)
    if not path:
        return jsonify(error='结果文件不存在'), 404
    job = store.get(owner(), job_id)
    filename = secure_filename(Path(job['filename']).stem) or 'translation'
    return send_file(path, as_attachment=True, download_name=filename + '_translated' + path.suffix)


@api.get('/jobs/<job_id>/preview')
def job_preview(job_id):
    path = current_app.extensions['jobs'].result_path(owner(), job_id)
    if not path:
        return jsonify(error='结果文件不存在'), 404
    result = inspect_file(path)
    for sheet in result['sheets']:
        sheet['rows'] = sheet.pop('preview')
    return jsonify(result)


@api.get('/jobs/<job_id>/manual-result')
def manual_result(job_id):
    """Return all translated manual rows, scoped to the owning browser/user."""
    store = current_app.extensions['jobs']
    job = store.get(owner(), job_id)
    if not job or job.get('input_mode') != 'manual':
        return jsonify(error='手动翻译结果不存在'), 404
    path = store.result_path(owner(), job_id)
    if path is None:
        return jsonify(error='手动翻译结果尚未完成'), 404
    mapping = store.manual_mapping(owner(), job_id)
    if not mapping:
        return jsonify(error='手动翻译结果不存在'), 404
    source, context, target = (mapping.get(key) for key in ('source', 'context', 'target'))
    if not all(isinstance(value, str) and value for value in (source, context, target)):
        raise ValueError('手动翻译结果格式无效')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {source, context, target}.issubset(reader.fieldnames):
            raise ValueError('手动翻译结果格式无效')
        rows = [{'source_text': row.get(source) or '', 'context': row.get(context) or '',
                 'translated_text': row.get(target) or ''} for row in reader]
    return jsonify(rows=rows)



def valid_pairs(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError('术语数据无效或过多')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('术语数据无效')
        source, target, note = (item.get(k, '') for k in ('source', 'target', 'note'))
        if not all(isinstance(v, str) for v in (source, target, note)):
            raise ValueError('术语数据无效')
        if source.strip() and target.strip():
            result.append({'source': source.strip(), 'target': target.strip(), 'note': note.strip()})
    if not result:
        raise ValueError('请至少填写一组源词和译文')
    return result


@api.post('/projects/<project_id>/glossary/check')
def glossary_check(project_id):
    data = payload()
    project, lang = scoped_project(project_id, data.get('language'))
    pairs = valid_pairs(data.get('pairs'))
    db = engine()
    # Do not use the legacy helper: it converts database errors to "no conflicts".
    existing = []
    sources = list(dict.fromkeys(p['source'] for p in pairs))
    for start in range(0, len(sources), 100):
        records = (db.client.table('glossaries').select('source_term,target_term,note')
                   .eq('game_id', project['database_id'].lower().strip())
                   .eq('target_language', lang.lower().strip())
                   .in_('source_term', sources[start:start + 100]).execute().data or [])
        existing.extend(records)
    lookup = {x['source_term']: x for x in existing}
    conflicts, clean = [], []
    for pair in pairs:
        old = lookup.get(pair['source'])
        if old:
            conflicts.append({'source': pair['source'], 'existing_target': old['target_term'], 'existing_note': old.get('note') or '', 'new_target': pair['target'], 'new_note': pair['note']})
        else:
            clean.append(pair)
    return jsonify(conflicts=conflicts, clean_pairs=clean)


@api.post('/projects/<project_id>/glossary/save')
def glossary_save(project_id):
    data = payload()
    project, lang = scoped_project(project_id, data.get('language'))
    count = engine().upsert_terms(project['database_id'], lang, valid_pairs(data.get('pairs')))
    return jsonify(count=count)


@api.post('/projects/<project_id>/knowledge/upload')
def knowledge_upload(project_id):
    project, lang = scoped_project(project_id, request.form.get('language'))
    kind = request.form.get('kind')
    if kind not in {'terms', 'translation_memory'}:
        raise ValueError('知识库类型无效')
    _, ext, raw = read_upload(request.files.get('file'))
    # Validate the same workbook limits as translation before pandas loads it.
    with tempfile.TemporaryDirectory(dir=current_app.config['STORAGE_ROOT']) as folder:
        path = Path(folder) / ('input' + ext)
        path.write_bytes(raw)
        sheets = inspect_file(path)['sheets']
    sheet_name = 'CSV_Sheet' if ext == '.csv' else (request.form.get('sheet') or sheets[0]['name'])
    selected = next((sheet for sheet in sheets if sheet['name'] == sheet_name), None)
    if not selected:
        raise ValueError('选择的工作表不存在')
    if selected['row_count'] > 10000:
        raise ValueError('每次最多上传 10000 行')
    import pandas as pd
    frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False) if ext == '.csv' else pd.read_excel(io.BytesIO(raw), sheet_name=sheet_name, dtype=str, keep_default_na=False)
    frame.columns = [str(column).strip() for column in frame.columns]
    source, target, note = (request.form.get(k) or '' for k in ('source', 'target', 'note'))
    if source not in frame.columns or target not in frame.columns or (note and note not in frame.columns):
        raise ValueError('选择的列不存在')
    if len(frame) > 10000:
        raise ValueError('每次最多上传 10000 行')
    pairs = valid_pairs([{'source': str(row[source]), 'target': str(row[target]), 'note': str(row[note]) if note else ''} for _, row in frame.iterrows()])
    db = engine()
    count = db.upsert_terms(project['database_id'], lang, pairs) if kind == 'terms' else db.upsert_tm(project['database_id'], lang, pairs)
    return jsonify(count=count)
