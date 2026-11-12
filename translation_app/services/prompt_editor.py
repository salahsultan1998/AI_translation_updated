"""Shared production prompts and read-only local development defaults."""
from httpx import HTTPError
from postgrest.exceptions import APIError

from . import projects


class PromptConflict(Exception):
    pass


class PromptUnavailable(RuntimeError):
    pass


def read_prompts(project_id, language, database=None):
    if database is None:
        return {kind: {'content': content, 'version': None}
                for kind, content in projects.load_prompt_pair(project_id, language).items()}
    projects.validate_selection(project_id, language)
    try:
        rows = (database.client.table('prompt_configs').select('kind,content,version')
                .eq('project_id', project_id).eq('target_language', language).execute().data)
    except (APIError, HTTPError) as exc:
        raise PromptUnavailable('无法从 Supabase 读取提示词，请稍后重试。') from exc
    entries = {row['kind']: {'content': row['content'], 'version': row['version']} for row in rows}
    if entries.keys() != {'system', 'instructions'}:
        raise PromptUnavailable('该项目语言的 Supabase 提示词配置不完整，请联系管理员。')
    return entries


def save_prompt(project_id, language, kind, content, version, database):
    projects.validate_selection(project_id, language)
    if (kind not in ('system', 'instructions') or not isinstance(content, str)
            or '\x00' in content or len(content.encode('utf-8')) > 200000):
        raise ValueError('提示词类型或内容无效')
    if type(version) is not int or not 1 <= version < 2**63:
        raise ValueError('提示词版本无效，请刷新后重试')
    try:
        rows = (database.client.table('prompt_configs').update({'content': content})
                .eq('project_id', project_id).eq('target_language', language)
                .eq('kind', kind).eq('version', version).select('content,version').execute().data)
    except (APIError, HTTPError) as exc:
        raise PromptUnavailable('无法将提示词保存到 Supabase，请稍后重试。') from exc
    if not rows:
        raise PromptConflict()
    return rows[0]
