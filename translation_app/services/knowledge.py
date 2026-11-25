"""Scoped operations shared by the two project knowledge libraries."""
import csv
import io
from collections import Counter
from uuid import UUID

from httpx import TransportError
from postgrest.exceptions import APIError

from .resources import invalidate_resources

KINDS = {'terms', 'memory'}
STATUSES = {'active', 'pending', 'archived'}
FIELDS = ('source', 'target', 'scene', 'note', 'string_key')
MAX_IMPORT_ROWS = 10000
PAGE_SIZE = 30


class KnowledgeError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def access(client, game):
    memberships = (client.table('knowledge_project_members').select('can_edit')
                   .eq('game_id', game).limit(1).execute().data or [])
    if not memberships:
        raise KnowledgeError('你没有此项目知识库的访问权限。', 403)
    return memberships[0]['can_edit']


def entry_values(data, kind):
    if not isinstance(data, dict):
        raise ValueError('条目格式无效。')
    entry = {}
    for field, limit in zip(FIELDS, (20000, 20000, 200, 8000, 300)):
        value = data.get(field, '')
        if not isinstance(value, str) or '\x00' in value or len(value) > limit:
            raise ValueError('条目包含无效或过长的内容。')
        entry[field] = value.strip()
    if not entry['source'] or not entry['target']:
        raise ValueError('请填写原文和译文。')
    if kind == 'terms':
        entry['string_key'] = ''
    return entry


def entry_status(value):
    if not isinstance(value, str) or value not in STATUSES:
        raise ValueError('条目状态无效。')
    return value


def entry_refs(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('请选择 1 至 1000 条记录。')
    refs = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('条目标识无效。')
        try:
            entry_id = str(UUID(row['id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError('条目标识无效。') from None
        version = row.get('version')
        if type(version) is not int or not 1 <= version < 2**53 or entry_id in refs:
            raise ValueError('条目版本无效或重复。')
        refs[entry_id] = {'id': entry_id, 'version': version}
    return list(refs.values())


def paste_rows(text):
    if not isinstance(text, str) or len(text) > 4_000_000:
        raise ValueError('粘贴内容太长或格式无效。')
    try:
        reader = csv.reader(io.StringIO(text, newline=''), delimiter='\t', strict=True)
        previous_line = data_rows = 0
        for row in reader:
            line, previous_line = previous_line + 1, reader.line_num
            if (line == 1 and len(row) >= 2 and
                    row[0].strip().lower() in {'原文', '原文术语', '源文本', 'source', 'source_text'} and
                    row[1].strip().lower() in {'译文', '标准译法', '目标文本', 'target', 'target_text'}):
                continue
            data_rows += 1
            if data_rows > MAX_IMPORT_ROWS:
                raise ValueError(f'每次最多导入 {MAX_IMPORT_ROWS} 行。')
            if any(value.strip() for value in row):
                yield line, dict(zip(FIELDS, row)) if len(row) <= len(FIELDS) else None
    except csv.Error as exc:
        raise ValueError('粘贴的表格格式无效。') from exc


class KnowledgeStore:
    def __init__(self, db, game, language, kind):
        if kind not in KINDS:
            raise ValueError('知识库类型无效。')
        self.db, self.game, self.kind = db, game, kind
        self.scope = {'p_game': game, 'p_language': language, 'p_kind': kind}

    def require_access(self, edit=False):
        can_edit = access(self.db.client, self.game)
        if edit and not can_edit:
            raise KnowledgeError('你只有此项目知识库的读取权限。', 403)
        return can_edit

    def _rpc(self, name, **parameters):
        return self.db.client.rpc(name, {**self.scope, **parameters}).execute().data

    def listing(self, query, scene, status, offset, limit=PAGE_SIZE):
        return self._rpc('knowledge_list', p_query=query, p_scene=scene, p_status=status,
                         p_offset=offset, p_limit=limit)

    def mutate(self, change, *, rows=(), refs=(), status='active', origin='manual'):
        try:
            count = self._rpc('knowledge_mutate', p_action=change, p_rows=list(rows), p_refs=list(refs),
                              p_status=status, p_origin=origin)
        except APIError as exc:
            if exc.code == 'PT403':
                raise KnowledgeError('你没有此项目知识库的编辑权限。', 403) from exc
            if exc.code in {'PT409', '23505'}:
                text = ('相同原文和适用范围的条目已存在。' if change == 'create' or exc.code == '23505'
                        else '部分条目已被修改或删除。请刷新列表并重新选择。')
                raise KnowledgeError(text, 409) from exc
            raise
        except TransportError:
            # The database may have committed before the connection failed.
            invalidate_resources(self.game, storage_root=self.db.storage_root)
            raise
        if count:
            invalidate_resources(self.game, storage_root=self.db.storage_root)
        return count

    def plan_import(self, rows):
        previews, candidates = [], {}
        for line, raw in rows:
            try:
                entry = entry_values(raw, self.kind)
            except ValueError as exc:
                source = raw.get('source', '') if isinstance(raw, dict) else ''
                previews.append({'line': line, 'status': 'invalid', 'reason': str(exc),
                                 'source': source[:240] if isinstance(source, str) else ''})
                continue
            entry['line'] = line
            scope = ('key', entry['string_key']) if entry['string_key'] else ('scene', entry['scene'])
            key = (entry['source'], scope)
            if key in candidates:
                previews.append(_compare(entry, candidates[key], '同一批次已有同范围原文'))
            else:
                candidates[key] = entry
        matches = {row['line']: row for row in self._rpc(
            'knowledge_import_matches', p_rows=list(candidates.values()))} if candidates else {}
        additions = []
        for entry in candidates.values():
            match = matches.get(entry['line'])
            if match:
                previews.append(_compare(entry, match, '保留已有条目'))
            else:
                previews.append({**entry, 'status': 'new'})
                additions.append({field: entry[field] for field in FIELDS})
        counts = Counter(row['status'] for row in previews)
        # Keep the response bounded while giving each category a visible sample.
        examples = []
        for status in ('conflict', 'invalid', 'new', 'duplicate'):
            category = sorted((row for row in previews if row['status'] == status), key=lambda row: row['line'])
            examples.extend(category[:10])
        for row in examples:
            for field in ('source', 'target', 'existing_target'):
                if row.get(field) and len(row[field]) > 240:
                    row[field] = row[field][:240] + '…'
            row.pop('note', None)
        return additions, {'counts': dict(counts), 'rows': examples, 'total': len(previews)}


def _compare(entry, existing, reason):
    unchanged = all(entry[field] == existing[field] for field in ('target', 'note', 'scene'))
    return {**entry, 'existing_target': existing['target'], 'reason': reason,
            'status': 'duplicate' if unchanged else 'conflict'}
