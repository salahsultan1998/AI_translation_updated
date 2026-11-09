"""Edit only configured prompt files, with cross-process conflict detection."""
import fcntl
import hashlib
import os
import tempfile

from . import projects


class PromptConflict(Exception):
    pass


def _paths(project_id, language):
    _, info = projects.validate_selection(project_id, language)
    folder = projects.CONFIG_ROOT / 'projects' / project_id
    lang_folder = folder / 'languages' / language
    return {'system': projects._safe_file(lang_folder, info['system_prompt']),
            'instructions': projects._safe_file(lang_folder, info['instructions'])}


def _entry(path):
    data = path.read_bytes()
    return {'content': data.decode('utf-8'), 'version': hashlib.sha256(data).hexdigest()}


def read_prompts(project_id, language):
    return {name: {**_entry(path), 'path': str(path.relative_to(projects.CONFIG_ROOT.resolve().parent))}
            for name, path in _paths(project_id, language).items()}


def save_prompt(project_id, language, kind, content, version):
    paths = _paths(project_id, language)
    if kind not in paths or not isinstance(content, str) or len(content.encode('utf-8')) > 200000:
        raise ValueError('提示词类型或内容无效')
    if not isinstance(version, str) or len(version) != 64:
        raise ValueError('提示词版本无效，请刷新后重试')
    path = paths[kind]
    lock_path = path.parent / ('.' + path.name + '.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _entry(path)['version'] != version:
            raise PromptConflict()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp_path = temp.name
                temp.write(content.encode('utf-8'))
                temp.flush()
                os.fsync(temp.fileno())
            os.chmod(temp_path, path.stat().st_mode & 0o777)
            os.replace(temp_path, path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
    return _entry(path)
