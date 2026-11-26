"""Authenticated endpoints used by the Flask workspace."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import zipfile

from flask import Blueprint, current_app, g, jsonify, request, send_file, url_for
from werkzeug.utils import secure_filename

from translation_app.services.knowledge import (
    FIELDS, MAX_IMPORT_ROWS, PAGE_SIZE, STATUSES, KnowledgeError, KnowledgeStore,
    entry_refs, entry_status, entry_values, paste_rows,
)
from translation_app.services.projects import get_project, validate_selection
from translation_app.services.prompt_editor import PromptConflict, PromptUnavailable, prompt_settings, read_prompts, save_prompt
from translation_app.services.settings import translation_settings
from translation_app.services.spreadsheets import has_mapped_rows, inspect_file, read_knowledge_rows

api = Blueprint('api', __name__, url_prefix='/api')


@api.errorhandler(ValueError)
def bad_input(error):
    return jsonify(error=str(error)), 400


@api.errorhandler(PromptUnavailable)
def prompt_unavailable(error):
    current_app.logger.warning('Prompt storage unavailable: %s', type(error.__cause__).__name__)
    return jsonify(error=str(error)), 503


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


@api.post('/projects/<project_id>/knowledge/preload')
def preload_knowledge(project_id):
    data = payload()
    project, language = validate_selection(project_id, data.get('language'))
    if project['direct']:
        raise ValueError('直接翻译不使用项目知识库')
    from translation_app.services.resources import preload_resources
    settings = {'access_token': g.user['access_token'],
                '_limiter_root': str(current_app.extensions['jobs'].root / 'provider-slots')}
    return jsonify(preload_resources(project, language, settings, include_lists=data.get('include_lists') is True,
                                     refresh=data.get('refresh') is True))


def upload_root():
    key = hashlib.sha256(owner().encode()).hexdigest()
    path = Path(current_app.config['STORAGE_ROOT']) / 'uploads' / key
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def upload_folder(upload_id):
    if not isinstance(upload_id, str) or not re.fullmatch(r'[0-9a-f]{32}', upload_id):
        raise ValueError('上传文件不存在，请重新上传')
    return upload_root() / upload_id


def upload_path(upload_id):
    folder = upload_folder(upload_id)
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


def expire_uploads():
    now = time.time()
    for path in upload_root().iterdir():
        try:
            if re.fullmatch(r'[0-9a-f]{32}', path.name) and now - path.stat().st_mtime > 24 * 3600:
                shutil.rmtree(path, ignore_errors=True)
            elif re.fullmatch(r'knowledge-[0-9a-f]{32}\.json', path.name) and now - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue


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


@api.delete('/uploads/<upload_id>')
def remove_upload(upload_id):
    try:
        shutil.rmtree(upload_folder(upload_id))
    except FileNotFoundError:
        pass
    return jsonify(ok=True)


@api.post('/jobs')
def create_job():
    data = payload()
    project_id, language = data.get('project_id'), data.get('language')
    project, _ = validate_selection(project_id, language)
    settings = translation_settings(data)
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
            mappings[sheet['name']] = {k: sheet[k] for k in ('source', 'context', 'target', 'target_mode') if k in sheet}
        # Preview fully checked this private upload; avoid materializing it again.
        if not has_mapped_rows(path, mappings):
            raise ValueError('未找到需要翻译的文本')
    if len(raw) > current_app.config['MAX_CONTENT_LENGTH']:
        raise ValueError('输入内容过大')
    pair = read_prompts(project_id, language, engine() if current_app.config['AUTH_ENABLED'] else None)
    settings.update(prompt_settings(pair))
    if not project['direct']:
        settings['access_token'] = g.user['access_token']
    store = current_app.extensions['jobs']
    if sum(item['status'] in {'queued', 'running', 'cancelling'} for item in store.list(owner(), limit=100)) >= 3:
        raise ValueError('同时最多提交三个任务，请等待已有任务完成')
    job = store.create(owner(), project_id, language, filename, raw, mappings, settings)
    return jsonify(job), 202


@api.route('/projects/<project_id>/prompts', methods=['GET', 'POST'])
def prompts(project_id):
    enabled = current_app.config['AUTH_ENABLED']
    if request.method == 'GET':
        return jsonify(prompts=read_prompts(project_id, request.args.get('language'), engine() if enabled else None),
                       read_only=not enabled)
    if not enabled:
        return jsonify(error='登录后才能保存 Supabase 提示词。'), 403
    data = payload()
    try:
        saved = save_prompt(project_id, data.get('language'), data.get('kind'), data.get('content'), data.get('version'), engine())
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
    expire_uploads()
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


@api.post('/jobs/<job_id>/cancel')
def cancel_job(job_id):
    job = current_app.extensions['jobs'].cancel(owner(), job_id)
    if job is None:
        return jsonify(error='任务不存在'), 404
    return jsonify(job), 202 if job['status'] == 'cancelling' else 200


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


@api.errorhandler(KnowledgeError)
def knowledge_error(error):
    return jsonify(error=str(error)), error.status


def knowledge_store(project_id, kind, language):
    project, lang = validate_selection(project_id, language)
    if project['direct']:
        raise ValueError('直接翻译没有项目知识库。')
    if not g.user['access_token']:
        raise KnowledgeError('请登录有项目权限的账号后使用知识库。', 403)
    return KnowledgeStore(engine(), project['database_id'].strip().lower(), lang['database_name'].strip().lower(), kind)


def knowledge_filters():
    query = request.args.get('q', '').strip()
    scene = request.args.get('scene', '').strip()
    status = request.args.get('status', 'current')
    if len(query) > 300 or len(scene) > 200 or '\x00' in (query + scene) or status not in STATUSES | {'current'}:
        raise ValueError('搜索或筛选条件无效。')
    return query, scene, status


@api.get('/projects/<project_id>/knowledge/<kind>/entries')
def knowledge_entries(project_id, kind):
    store = knowledge_store(project_id, kind, request.args.get('language'))
    editable = store.require_access()
    filters = knowledge_filters()
    page = request.args.get('page', '1')
    if not re.fullmatch(r'[1-9][0-9]{0,4}', page):
        raise ValueError('页码无效。')
    return jsonify(**store.listing(*filters, (int(page) - 1) * PAGE_SIZE), editable=editable, page_size=PAGE_SIZE)


@api.get('/projects/<project_id>/knowledge/<kind>/snapshot')
def knowledge_snapshot(project_id, kind):
    store = knowledge_store(project_id, kind, request.args.get('language'))
    editable = store.require_access()
    return jsonify(rows=store.snapshot_rows(), editable=editable, page_size=PAGE_SIZE)


@api.get('/projects/<project_id>/knowledge/<kind>/export')
def knowledge_export(project_id, kind):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    store = knowledge_store(project_id, kind, request.args.get('language'))
    store.require_access()
    filters = knowledge_filters()
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('知识库')
    if kind == 'memory':
        sheet.append(['原文', '译文', '适用场景', '备注', '字符串 Key', '状态'])
        fields = FIELDS
    else:
        sheet.append(['原文', '译文', '适用场景', '备注', '状态'])
        fields = FIELDS[:-1]
    names = {'active': '可用', 'pending': '待确认', 'archived': '已归档'}
    offset = 0
    while True:
        result = store.listing(*filters, offset, 100)
        for row in result['rows']:
            cells = [WriteOnlyCell(sheet, value=value) for value in
                     [*(row[field] for field in fields), names[row['status']]]]
            # Preserve formula-looking strings as text, including on re-import.
            for cell in cells:
                cell.data_type = 's'
            sheet.append(cells)
        offset += len(result['rows'])
        if offset >= result['total'] or not result['rows']:
            break
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f'{project_id}-{kind}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@api.post('/projects/<project_id>/knowledge/<kind>/entries')
def knowledge_create(project_id, kind):
    data = payload()
    store = knowledge_store(project_id, kind, data.get('language'))
    store.require_access(edit=True)
    entry = entry_values(data.get('entry'), kind)
    count = store.mutate('create', rows=[entry], status=entry_status(data.get('status', 'active')))
    return jsonify(count=count), 201


@api.patch('/projects/<project_id>/knowledge/<kind>/entries/<entry_id>')
def knowledge_edit(project_id, kind, entry_id):
    data = payload()
    store = knowledge_store(project_id, kind, data.get('language'))
    store.require_access(edit=True)
    entry = entry_values(data.get('entry'), kind)
    refs = entry_refs([{'id': entry_id, 'version': data.get('version')}])
    return jsonify(count=store.mutate('edit', rows=[entry], refs=refs, status=entry_status(data.get('status'))))


@api.post('/projects/<project_id>/knowledge/<kind>/bulk')
def knowledge_bulk(project_id, kind):
    data = payload()
    store = knowledge_store(project_id, kind, data.get('language'))
    store.require_access(edit=True)
    change = data.get('action')
    if change not in ('status', 'delete'):
        raise ValueError('批量操作无效。')
    refs = entry_refs(data.get('entries'))
    status = entry_status(data.get('status')) if change == 'status' else 'active'
    return jsonify(count=store.mutate(change, refs=refs, status=status))


@api.post('/projects/<project_id>/knowledge/<kind>/import/preview')
def knowledge_import_preview(project_id, kind):
    data = payload()
    store = knowledge_store(project_id, kind, data.get('language'))
    store.require_access(edit=True)
    if 'paste' in data:
        origin, rows = 'paste', paste_rows(data['paste'])
    else:
        path, _ = upload_path(data.get('upload_id'))
        mapping, sheet = data.get('mapping'), data.get('sheet')
        if (not isinstance(mapping, dict) or not isinstance(sheet, str) or
                any(key not in FIELDS or not isinstance(value, str) for key, value in mapping.items()) or
                not mapping.get('source') or not mapping.get('target')):
            raise ValueError('请选择有效的原文和译文列。')
        origin, rows = 'file', read_knowledge_rows(path, sheet, mapping, MAX_IMPORT_ROWS)
    try:
        additions, preview = store.plan_import(rows)
    except UnicodeDecodeError as exc:
        raise ValueError('CSV 编码无效，请另存为 UTF-8。') from exc
    plan_id = uuid.uuid4().hex if additions else None
    if plan_id:
        plan = {'scope': store.scope, 'origin': origin, 'created': time.time(), 'rows': additions}
        path = upload_root() / f'knowledge-{plan_id}.json'
        with path.open('x', encoding='utf-8') as output:
            os.chmod(path, 0o600)
            json.dump(plan, output, ensure_ascii=False)
    return jsonify(**preview, plan_id=plan_id)


@api.post('/projects/<project_id>/knowledge/<kind>/import')
def knowledge_import(project_id, kind):
    data = payload()
    store = knowledge_store(project_id, kind, data.get('language'))
    store.require_access(edit=True)
    plan_id = data.get('plan_id')
    if not isinstance(plan_id, str) or not re.fullmatch(r'[0-9a-f]{32}', plan_id):
        raise ValueError('请重新预览导入内容。')
    path = upload_root() / f'knowledge-{plan_id}.json'
    try:
        plan = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        raise ValueError('请重新预览导入内容。') from None
    if plan['scope'] != store.scope or time.time() - plan['created'] > 3600:
        raise ValueError('预览已过期或所属知识库已切换，请重新预览。')
    count = store.mutate('import', rows=plan['rows'], status='pending', origin=plan['origin'])
    return jsonify(count=count, skipped=len(plan['rows']) - count)
