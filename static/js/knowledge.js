(() => {
  'use strict';

  const libraries = {
    terms: { name: '术语库', item: '术语', source: '原文术语', target: '标准译法', note: '使用说明', description: '项目内统一使用的词语和短语' },
    memory: { name: '历史翻译', item: '历史翻译', source: '原文', target: '译文', note: '上下文或备注', description: '由团队手动收录的整句译文' }
  };
  const statuses = { active: '可用', pending: '待确认', archived: '已归档' };
  const origins = { legacy: '历史资料', manual: '手动添加', paste: '批量粘贴', file: '文件导入' };
  const previewLabels = { new: '新增', duplicate: '相同', conflict: '冲突', invalid: '无效' };
  const date = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
  const node = (tag, text = '', className = '') => {
    const item = document.createElement(tag);
    item.textContent = text;
    if (className) item.className = className;
    return item;
  };

  window.createKnowledgeLibrary = ({ panel, projectId, language, request, snapshot, changed }) => {
    const $ = id => document.getElementById('kb-' + id);
    const tabs = [...panel.querySelectorAll('[data-kind]')];
    const form = $('form');
    const selected = new Map();
    const state = { kind: 'terms', language: language.value, page: 1, pages: 1, rows: [],
      editable: false, loading: false, sequence: 0, writing: false, reading: false };
    let noticeTimer, editing, importFile, plan, importMode, importSequence = 0;
    const path = (part = '') => `/api/projects/${encodeURIComponent(projectId)}/knowledge/${state.kind}/${part}`;
    const params = () => new URLSearchParams({
      language: state.language, q: $('search').value, scene: $('scenes').value, status: $('filter').value
    });
    const formField = name => form.elements.namedItem(name);

    function message(text = '', error = false) {
      clearTimeout(noticeTimer);
      $('error').hidden = !error || !text;
      $('notice').hidden = error || !text;
      $(error ? 'error' : 'notice').textContent = text;
      if (text && !error) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 4500);
    }

    function dialogMessage(dialog, text = '') {
      const error = dialog.querySelector('[data-dialog-error]');
      error.textContent = text;
      error.hidden = !text;
    }

    function syncSelection() {
      const active = state.editable && !state.loading && !state.writing;
      const checks = [...$('rows').querySelectorAll('input[data-id]')];
      let onPage = 0;
      for (const check of checks) {
        check.checked = selected.has(check.dataset.id);
        check.disabled = !active;
        check.closest('tr').dataset.selected = String(check.checked);
        if (check.checked) onPage++;
      }
      $('select-page').disabled = !active || !checks.length;
      $('select-page').checked = Boolean(checks.length && onPage === checks.length);
      $('select-page').indeterminate = onPage > 0 && onPage < checks.length;
      $('bulk').hidden = !state.editable || !selected.size;
      $('selected').textContent = `已选 ${selected.size} 条` +
        (selected.size === onPage ? '' : ` · 本页 ${onPage} 条`);
      if (!selected.size) $('bulk-status').value = '';
      for (const id of ['clear-selection', 'bulk-status', 'delete']) $(id).disabled = !active;
      $('apply-status').disabled = !active || !selected.size || !$('bulk-status').value;
      $('prev').disabled = state.loading || state.writing || state.page <= 1;
      $('next').disabled = state.loading || state.writing || state.page >= state.pages;
    }

    function renderRows() {
      const arabic = state.language === 'arabic';
      const records = state.rows.map(row => {
        const tr = node('tr');
        const checkCell = tr.insertCell();
        checkCell.className = 'knowledge-select';
        if (state.editable) {
          const label = node('label');
          const input = node('input');
          input.type = 'checkbox';
          input.dataset.id = row.id;
          input.setAttribute('aria-label', `选择 ${row.source}${row.scene ? ' · ' + row.scene : ''}`);
          input.addEventListener('change', () => toggleRows([row], input.checked));
          label.append(input);
          checkCell.append(label);
        }
        const source = tr.insertCell();
        source.textContent = row.source;
        if (row.string_key) source.append(node('span', row.string_key, 'knowledge-key'));
        if (row.scene || row.string_key) source.append(node('span', row.scene || row.string_key, 'knowledge-mobile-scene'));
        const target = node('bdi', row.target, 'knowledge-target');
        target.dir = arabic ? 'rtl' : 'auto';
        if (arabic) target.lang = 'ar';
        tr.insertCell().append(target);
        const scene = tr.insertCell();
        scene.className = 'knowledge-scene';
        scene.textContent = row.scene || '通用';
        const status = node('span', statuses[row.status], 'knowledge-state-mark');
        status.dataset.state = row.status;
        const statusCell = tr.insertCell();
        statusCell.className = 'knowledge-state';
        statusCell.append(status);
        const dateCell = tr.insertCell();
        dateCell.className = 'knowledge-date';
        dateCell.textContent = date.format(new Date(row.updated_at));
        const editCell = tr.insertCell();
        editCell.className = 'knowledge-row-action';
        const edit = node('button', state.editable ? '编辑' : '查看', 'text-button compact');
        edit.type = 'button';
        edit.setAttribute('aria-label', `${edit.textContent} ${row.source}${row.scene ? ' · ' + row.scene : ''}`);
        edit.addEventListener('click', () => {
          if (!state.loading && !state.writing) openEditor(row);
        });
        editCell.append(edit);
        return tr;
      });
      $('rows').replaceChildren(...records);
      syncSelection();
    }

    async function snapshotPage(kind, search) {
      const { libraries, editable, page_size } = await snapshot(search.get('language'));
      const { rows, scenes } = libraries[kind];
      const status = search.get('status');
      const scene = search.get('scene');
      const query = (search.get('q') || '').trim().toLowerCase();
      const matching = rows.filter(row =>
        (status === 'current' ? row.status !== 'archived' : row.status === status) &&
        (!scene || row.scene === scene) &&
        (!query || row.source.toLowerCase().includes(query) || row.target.toLowerCase().includes(query) ||
          (kind === 'memory' && row.string_key.toLowerCase().includes(query))));
      const start = (Number(search.get('page')) - 1) * page_size;
      return { editable, page_size, scenes, rows: matching.slice(start, start + page_size), total: matching.length };
    }

    async function load() {
      const sequence = ++state.sequence;
      state.loading = true;
      $('retry').hidden = true;
      $('export').hidden = true;
      $('empty').hidden = Boolean(state.rows.length);
      $('empty').textContent = '正在加载知识库…';
      syncSelection();
      const search = params();
      search.set('page', state.page);
      try {
        const result = await snapshotPage(state.kind, search);
        if (sequence !== state.sequence) return;
        state.pages = Math.max(1, Math.ceil(result.total / result.page_size));
        if (state.page > state.pages) {
          state.page = state.pages;
          return load();
        }
        state.editable = result.editable;
        state.loading = false;
        if (!state.editable) selected.clear();
        state.rows = result.rows;
        $('write-actions').hidden = !state.editable;
        const scene = $('scenes').value;
        $('scenes').replaceChildren(new Option('全部场景', ''), ...result.scenes.map(value => new Option(value, value)));
        $('scenes').value = result.scenes.includes(scene) ? scene : '';
        renderRows();
        $('empty').textContent = '没有找到条目';
        $('empty').hidden = Boolean(result.rows.length);
        const first = (state.page - 1) * result.page_size + 1;
        $('count').textContent = result.total ? `显示 ${first}–${first + result.rows.length - 1} 条，共 ${result.total} 条` : '共 0 条';
        $('pages').textContent = `${state.page} / ${state.pages}`;
        $('export').href = path('export') + '?' + search;
        $('export').hidden = !result.rows.length;
        $('retry').textContent = '刷新';
        $('retry').hidden = false;
      } catch (error) {
        if (sequence !== state.sequence) return;
        state.loading = false;
        state.editable = false;
        state.rows = [];
        selected.clear();
        $('write-actions').hidden = true;
        $('empty').textContent = '知识库暂时无法加载';
        $('empty').hidden = false;
        $('count').textContent = '';
        $('pages').textContent = '1 / 1';
        state.page = state.pages = 1;
        $('retry').textContent = '重新加载';
        $('retry').hidden = false;
        renderRows();
        message(error.message, true);
      }
    }

    function resetFilters() {
      selected.clear();
      state.page = 1;
      state.rows = [];
      $('count').textContent = '';
      $('empty').hidden = false;
      $('empty').textContent = '正在加载知识库…';
      state.loading = true;
      $('export').hidden = true;
      state.sequence++;
      renderRows();
    }

    function setScope(kind) {
      state.kind = kind;
      state.language = language.value;
      state.editable = false;
      $('write-actions').hidden = true;
      const info = libraries[kind];
      for (const tab of tabs) {
        const active = tab.dataset.kind === kind;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        if (active) $('library').setAttribute('aria-labelledby', tab.id);
      }
      $('language').textContent = language.selectedOptions[0].textContent;
      $('title').textContent = info.name;
      $('description').textContent = info.description;
      $('source-heading').textContent = info.source;
      $('target-heading').textContent = info.target;
      $('search').placeholder = kind === 'memory' ? '搜索原文、译文或 Key' : '搜索原文或译文';
      $('search').value = '';
      $('scenes').replaceChildren(new Option('全部场景', ''));
      $('filter').value = 'current';
      $('new').textContent = '新增' + info.item;
      message();
      resetFilters();
      if (!panel.hidden) load();
    }

    function toggleRows(rows, checked) {
      if (checked) {
        const additions = rows.filter(row => !selected.has(row.id));
        if (selected.size + additions.length > 1000) {
          message('每次最多选择 1000 条记录。', true);
        } else {
          for (const row of additions) selected.set(row.id, row);
        }
      } else {
        for (const row of rows) selected.delete(row.id);
      }
      syncSelection();
    }

    function focusSelect() {
      ($('select-page').disabled ? $('search') : $('select-page')).focus();
    }

    function refs(rows) {
      return [...rows].map(({ id, version }) => ({ id, version }));
    }

    async function mutation(part, method, body, success, dialog = null) {
      if (state.writing || state.loading || !state.editable) return;
      const scope = path();
      const activeLanguage = state.language;
      const inScope = () => scope === path() && activeLanguage === state.language;
      state.writing = true;
      syncSelection();
      const controls = dialog ? [...dialog.querySelectorAll('button,input,textarea,select')].filter(control => !control.disabled) : [];
      for (const control of controls) control.disabled = true;
      message();
      if (dialog) dialogMessage(dialog);
      try {
        const result = await request(path(part), { method, body: { language: activeLanguage, ...body } });
        if (result.count) changed(activeLanguage);
        if (!inScope()) return;
        if (dialog) dialog.close();
        selected.clear();
        message(success(result));
        await load();
      } catch (error) {
        if (!inScope()) return;
        if (error.status === 409 || error.status === 403) {
          selected.clear();
          changed(activeLanguage, true);
          await load();
        }
        if (dialog) dialogMessage(dialog, error.message);
        else message(error.message, true);
      } finally {
        state.writing = false;
        for (const control of controls) control.disabled = false;
        if (state.editable === false) {
          $('save').hidden = true;
          $('archive').hidden = true;
        }
        syncSelection();
        if (!dialog && !selected.size && inScope()) focusSelect();
      }
    }

    function openEditor(row = null) {
      editing = row;
      form.reset();
      const info = libraries[state.kind];
      const dialog = $('editor');
      dialogMessage(dialog);
      $('editor-title').textContent = (row ? (state.editable ? '编辑' : '查看') : '新增') + info.item;
      $('source-label').textContent = info.source;
      $('target-label').textContent = info.target;
      $('note-label').textContent = info.note;
      $('key-field').hidden = state.kind !== 'memory';
      formField('target').dir = state.language === 'arabic' ? 'rtl' : 'auto';
      for (const name of ['source', 'target', 'scene', 'note', 'status', 'string_key']) {
        const field = formField(name);
        field.disabled = !state.editable;
        if (row) field.value = row[name];
      }
      $('entry-meta').hidden = !row;
      if (row) {
        const reference = row.status === 'active' && !row.verified && state.kind === 'memory' ? ' · 仅供参考' : '';
        $('entry-meta').textContent = `${origins[row.origin]}${reference} · 更新于 ${date.format(new Date(row.updated_at))}`;
      }
      $('archive').hidden = !row || !state.editable;
      $('archive').textContent = row?.status === 'archived' ? '恢复为可用' : '归档条目';
      $('save').hidden = !state.editable;
      dialog.showModal();
      if (state.editable) formField('source').focus();
    }

    function openDelete() {
      if (!selected.size) return;
      const dialog = $('delete-dialog');
      dialogMessage(dialog);
      const rows = [...selected.values()];
      $('delete-title').textContent = `删除 ${rows.length} 条${libraries[state.kind].item}？`;
      const preview = rows.slice(0, 4).map(row => {
        const li = node('li', '', 'knowledge-preview-line');
        li.append(node('span', row.source.slice(0, 180)), node('span', [row.scene, row.string_key].filter(Boolean).join(' · ') || '通用', 'muted'));
        return li;
      });
      if (rows.length > preview.length) preview.push(node('li', `另有 ${rows.length - preview.length} 条已选`, 'muted'));
      $('delete-rows').replaceChildren(...preview);
      dialog.showModal();
      $('cancel-delete').focus();
    }

    const importDialog = $('import-dialog');
    const columns = [...panel.querySelectorAll('[data-mapping]')];
    const aliases = {
      source: ['原文', '原文术语', '源文本', '原文文本', 'source', 'source_text'],
      target: ['译文', '标准译法', '目标文本', 'target', 'target_text'],
      scene: ['适用场景', '场景', 'scene'],
      note: ['备注', '使用说明', '上下文', 'note', 'context'],
      string_key: ['字符串 key', 'key', 'string_key']
    };

    function resetPreview() {
      importSequence++;
      state.reading = false;
      plan = null;
      $('preview-result').hidden = true;
      $('commit').disabled = true;
      $('preview').disabled = importMode === 'file' && !importFile;
      $('file-read').disabled = false;
      dialogMessage(importDialog);
    }

    function openImport(mode) {
      importMode = mode;
      importFile = null;
      $('import-title').textContent = (mode === 'paste' ? '批量粘贴' : '导入') + libraries[state.kind].item;
      $('paste-field').hidden = mode !== 'paste';
      $('file-field').hidden = mode !== 'file';
      $('mapping').hidden = true;
      $('import-key').hidden = state.kind !== 'memory';
      $('paste-label').textContent = state.kind === 'memory'
        ? '从 Excel 粘贴：原文、译文、场景、备注、字符串 Key'
        : '从 Excel 粘贴：原文、译文、场景、备注';
      $('paste-text').value = '';
      $('file').value = '';
      resetPreview();
      importDialog.showModal();
    }

    function mapSheet() {
      const sheet = importFile?.sheets.find(item => item.name === $('sheet').value);
      if (!sheet) return;
      for (const select of columns) {
        const key = select.dataset.mapping;
        const headers = sheet.columns;
        select.replaceChildren(new Option('请选择', ''), ...headers.map(header => new Option(header, header)));
        const fallback = key === 'source' ? headers[0] : key === 'target' ? headers[1] : '';
        select.value = headers.find(header => aliases[key].includes(header.toLowerCase())) || fallback || '';
      }
      if ($('map-source').value === $('map-target').value) $('map-target').value = '';
      resetPreview();
    }

    async function previewRequest(call, success) {
      if (state.reading) return;
      const sequence = ++importSequence;
      state.reading = true;
      $('preview').disabled = true;
      $('file-read').disabled = true;
      dialogMessage(importDialog);
      try {
        const result = await call();
        if (sequence === importSequence && importDialog.open) success(result);
      } catch (error) {
        if (sequence === importSequence && importDialog.open) dialogMessage(importDialog, error.message);
      } finally {
        if (sequence === importSequence) {
          state.reading = false;
          $('preview').disabled = importMode === 'file' && !importFile;
          $('file-read').disabled = false;
        }
      }
    }

    function showPreview(result) {
      const counts = result.counts;
      const summary = Object.entries(previewLabels).map(([key, label]) => `${label} ${counts[key] || 0} 条`).join(' · ');
      $('preview-counts').textContent = summary + (result.total > result.rows.length ? '（下方展示部分条目）' : '');
      $('preview-rows').replaceChildren(...result.rows.map(row => {
        const li = node('li');
        const line = node('div', '', 'knowledge-preview-line');
        line.append(node('span', `第 ${row.line} 行 · ${row.source || '空白原文'}`), node('span', previewLabels[row.status], 'muted'));
        li.append(line);
        if (row.target) {
          const target = node('bdi', row.target);
          target.dir = 'auto';
          li.append(target);
        }
        if (row.existing_target && row.status === 'conflict') li.append(node('div', '已有译文 · ' + row.existing_target, 'muted'));
        if (row.reason) li.append(node('div', row.reason, 'muted'));
        return li;
      }));
      $('preview-result').hidden = false;
      plan = result.plan_id;
      $('commit').disabled = !plan;
    }

    for (const [index, tab] of tabs.entries()) {
      tab.addEventListener('click', () => { if (state.kind !== tab.dataset.kind) setScope(tab.dataset.kind); });
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : 1 - index];
        next.click();
        next.focus();
      });
    }
    $('search').addEventListener('input', () => {
      resetFilters();
      load();
    });
    for (const id of ['scenes', 'filter']) $(id).addEventListener('change', () => { resetFilters(); load(); });
    $('retry').addEventListener('click', () => {
      changed(state.language, true);
      message();
      load();
    });
    $('prev').addEventListener('click', () => { state.page--; load(); });
    $('next').addEventListener('click', () => { state.page++; load(); });
    $('select-page').addEventListener('change', event => toggleRows(state.rows, event.target.checked));
    $('clear-selection').addEventListener('click', () => { selected.clear(); syncSelection(); focusSelect(); });
    $('bulk-status').addEventListener('change', syncSelection);
    $('apply-status').addEventListener('click', () => {
      const status = $('bulk-status').value;
      if (!status || !selected.size) return;
      mutation('bulk', 'POST', { action: 'status', status, entries: refs(selected.values()) },
        result => result.count ? `已将 ${result.count} 条设为「${statuses[status]}」` : '选中条目的状态无需更改');
    });
    $('delete').addEventListener('click', openDelete);
    $('confirm-delete').addEventListener('click', () => {
      if (selected.size) mutation('bulk', 'POST', { action: 'delete', entries: refs(selected.values()) },
        result => `已删除 ${result.count} 条${libraries[state.kind].item}`, $('delete-dialog'));
    });
    $('new').addEventListener('click', () => { if (!state.loading && !state.writing) openEditor(); });
    form.addEventListener('submit', event => {
      event.preventDefault();
      const { status, ...entry } = Object.fromEntries(new FormData(form));
      mutation(editing ? 'entries/' + encodeURIComponent(editing.id) : 'entries', editing ? 'PATCH' : 'POST',
        { entry, status, version: editing?.version }, () => editing ? '已保存修改' : '已添加' + libraries[state.kind].item, $('editor'));
    });
    $('archive').addEventListener('click', () => {
      const status = editing.status === 'archived' ? 'active' : 'archived';
      mutation('bulk', 'POST', { action: 'status', status, entries: refs([editing]) },
        () => status === 'archived' ? '已归档，可通过筛选恢复' : '已恢复为可用', $('editor'));
    });
    for (const dialog of panel.querySelectorAll('dialog')) {
      for (const button of dialog.querySelectorAll('[data-close]')) button.addEventListener('click', () => dialog.close());
      dialog.addEventListener('cancel', event => { if (state.writing) event.preventDefault(); });
    }
    $('paste').addEventListener('click', () => openImport('paste'));
    $('import').addEventListener('click', () => openImport('file'));
    $('paste-text').addEventListener('input', resetPreview);
    $('file').addEventListener('change', () => {
      importFile = null;
      $('mapping').hidden = true;
      resetPreview();
    });
    $('file-read').addEventListener('click', () => {
      const file = $('file').files[0];
      if (!file) { dialogMessage(importDialog, '请选择文件。'); return; }
      importFile = null;
      $('mapping').hidden = true;
      resetPreview();
      const body = new FormData();
      body.append('file', file);
      previewRequest(() => request('/api/uploads/preview', { method: 'POST', body }), result => {
        importFile = result;
        $('sheet').replaceChildren(...result.sheets.map(sheet => new Option(sheet.name, sheet.name)));
        $('mapping').hidden = false;
        mapSheet();
      });
    });
    $('sheet').addEventListener('change', mapSheet);
    for (const control of columns) control.addEventListener('change', resetPreview);
    $('preview').addEventListener('click', () => {
      const fields = columns.filter(control => !control.closest('[hidden]'));
      const source = importMode === 'paste'
        ? { paste: $('paste-text').value }
        : { upload_id: importFile?.upload_id, sheet: $('sheet').value,
          mapping: Object.fromEntries(fields.map(control => [control.dataset.mapping, control.value])) };
      resetPreview();
      previewRequest(() => request(path('import/preview'), { method: 'POST', body: { language: state.language, ...source } }), showPreview);
    });
    $('commit').addEventListener('click', () => {
      if (plan) mutation('import', 'POST', { plan_id: plan }, result => `已新增 ${result.count} 条待确认记录` +
        (result.skipped ? ` · ${result.skipped} 条已存在，已保留原记录` : ''), importDialog);
    });
    importDialog.addEventListener('close', resetPreview);

    window.addEventListener('pagehide', () => { state.sequence++; });
    window.addEventListener('pageshow', event => {
      if (!event.persisted) return;
      snapshot(state.language).catch(() => {});
      if (!panel.hidden) load();
    });

    return {
      open() {
        if (state.language !== language.value) setScope(state.kind);
        else {
          $('language').textContent = language.selectedOptions[0].textContent;
          load();
        }
      },
      languageChanged() {
        if (state.language !== language.value) setScope(state.kind);
      }
    };
  };
})();
