(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const workspace = $('workspace');
  const projectId = workspace.dataset.project;
  const isDirect = workspace.dataset.direct === 'true';
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const state = { input: 'manual', upload: null, uploadSequence: 0, uploading: false, busy: false,
    timer: null, languageChanged: false, submitted: null };
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; };
  const workerHint = node('p', '', 'muted small');
  workerHint.setAttribute('role', 'status');
  workerHint.hidden = true;
  $('job-status').appendChild(workerHint);
  const resourcePreloads = new Map();
  const library = isDirect ? null : window.createKnowledgeLibrary({
    panel: $('knowledge-panel'), projectId, language: $('language'), request,
    snapshot: preloadResources, changed: refreshKnowledge
  });
  const knowledgeStatus = $('knowledge-status');
  function preloadResources(language, refresh = false) {
    const existing = resourcePreloads.get(language);
    if (existing) return existing.promise;
    const entry = {};
    resourcePreloads.set(language, entry);
    if ($('language').value === language) knowledgeStatus.textContent = '正在加载项目知识库…';
    entry.promise = request(`/api/projects/${encodeURIComponent(projectId)}/knowledge/preload`, {
      method: 'POST', body: {language, include_lists: true, refresh}
    }).then(result => {
      for (const [kind, rows] of Object.entries(result.libraries)) {
        rows.sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
        result.libraries[kind] = { rows, scenes: [...new Set(rows.map(row => row.scene).filter(Boolean))].sort() };
      }
      if ($('language').value === language && resourcePreloads.get(language) === entry) {
        showKnowledgeStatus(result);
      }
      return result;
    }).catch(error => {
      if (resourcePreloads.get(language) === entry) {
        resourcePreloads.delete(language);
        if ($('language').value === language) knowledgeStatus.textContent = '知识库加载失败，点击开始翻译时将重试。';
      }
      throw error;
    });
    return entry.promise;
  }
  function showKnowledgeStatus(result) {
    knowledgeStatus.textContent = result.expires_in > 0
      ? `知识库已就绪 · ${result.terms} 条术语 · ${result.memory} 条翻译记忆`
      : '知识库已加载，但缓存未启用，翻译时会重新加载。';
  }
  function refreshKnowledge(language, refresh = false) {
    resourcePreloads.delete(language);
    if ($('language').value === language) preloadResources(language, refresh).catch(() => {});
  }

  function showError(message) { $('error').textContent = message; $('error').hidden = false; $('notice').hidden = true; }
  function showNotice(message) { $('notice').textContent = message; $('notice').hidden = false; $('error').hidden = true; }
  function clearMessages() { $('error').hidden = true; $('notice').hidden = true; }
  async function request(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
    if (options.body && !(options.body instanceof FormData)) { headers.set('Content-Type', 'application/json'); options.body = JSON.stringify(options.body); }
    let response;
    try { response = await fetch(url, { ...options, headers, credentials: 'same-origin' }); }
    catch (_) { throw new Error('网络连接失败，请检查服务后重试。'); }
    let data; try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const error = new Error(data.error || '操作失败，请稍后重试。');
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function table(target, columns, rows) {
    target.replaceChildren();
    const head = target.createTHead().insertRow();
    columns.forEach(col => { const cell = node('th', col); cell.scope = 'col'; head.appendChild(cell); });
    const body = target.createTBody();
    rows.forEach(row => { const tr = body.insertRow(); columns.forEach((col, index) => { const value = Array.isArray(row) ? row[index] : row[col]; tr.appendChild(node('td', value == null ? '' : String(value))); }); });
  }
  function selectOptions(select, values, includeBlank = false) {
    select.replaceChildren();
    if (includeBlank) { const blank = node('option', '无'); blank.value = ''; select.appendChild(blank); }
    values.forEach(value => { const option = node('option', value); option.value = value; select.appendChild(option); });
  }
  function field(label, input) { const wrap = node('label', label); wrap.appendChild(input); return wrap; }
  const manualGrid = window.createManualGrid($('manual-rows'), {showError, showNotice});
  async function copyManual(translationsOnly) {
    const text = manualGrid.tsv(translationsOnly);
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const input = node('textarea'); input.value = text; input.setAttribute('readonly', '');
      input.className = 'clipboard-helper'; document.body.appendChild(input); input.select();
      let copied = false;
      try { copied = document.execCommand('copy'); } catch (_) { copied = false; } finally { input.remove(); }
      if (!copied) { showError('无法自动复制，请检查浏览器剪贴板权限。'); return; }
    }
    showNotice(translationsOnly ? '已复制全部译文，可粘贴到 Excel。' : '已复制整表，可粘贴到 Excel。');
  }
  const promptState = { entries: {}, language: null, loading: 0 };
  function promptsDirty() { return Object.values(promptState.entries).some(entry => entry.textarea.value !== entry.saved); }
  function promptMessage(message, error = false) { const box = $('prompt-message'); box.textContent = message; box.className = error ? 'notice error' : 'notice success'; box.hidden = false; }
  async function loadPrompts() {
    const language = $('language').value, requestId = ++promptState.loading;
    $('prompt-language-name').textContent = $('language').selectedOptions[0].textContent;
    $('prompt-message').hidden = true;
    $('prompt-editors').replaceChildren();
    promptState.entries = {}; promptState.language = language;
    try {
      const result = await request(`/api/projects/${encodeURIComponent(projectId)}/prompts?language=${encodeURIComponent(language)}`);
      if (requestId !== promptState.loading) return;
      const labels = {system:['System prompt（系统提示词）','定义翻译角色和核心规则'], instructions:['Instructions（翻译要求）','定义当前游戏和语言的风格、语气与术语规则']};
      for (const kind of ['system', 'instructions']) {
        const data = result.prompts[kind]; if (!data) continue;
        const section = node('div', null, 'prompt-editor');
        const heading = node('h3', labels[kind][0]); const description = node('p', labels[kind][1], 'muted small');
        const textarea = node('textarea'); textarea.value = data.content; textarea.setAttribute('aria-label', labels[kind][0]); textarea.dir = 'auto';
        if (result.read_only) {
          textarea.readOnly = true;
          section.append(heading, description, textarea);
          $('prompt-editors').appendChild(section);
          continue;
        }
        const actions = node('div', null, 'prompt-actions'); const status = node('span', '', 'muted small');
        const discard = node('button', '放弃修改', 'secondary compact'); discard.type = 'button'; discard.disabled = true;
        const save = node('button', '保存', 'compact'); save.type = 'button'; save.disabled = true;
        const entry = {textarea, saved: textarea.value, version: data.version}; promptState.entries[kind] = entry;
        function update() { const dirty = textarea.value !== entry.saved; status.textContent = dirty ? '未保存' : ''; save.disabled = !dirty; discard.disabled = !dirty; }
        textarea.addEventListener('input', update);
        discard.addEventListener('click', () => { textarea.value = entry.saved; update(); });
        save.addEventListener('click', async () => {
          const value = textarea.value; save.disabled = true; discard.disabled = true;
          try {
            const saved = await request(`/api/projects/${encodeURIComponent(projectId)}/prompts`, {method:'POST', body:{language, kind, content:value, version:entry.version}});
            entry.saved = saved.prompt.content; entry.version = saved.prompt.version; update(); promptMessage('提示词已保存');
          } catch (e) { update(); promptMessage(e.message, true); }
        });
        actions.append(status, discard, save); section.append(heading, description, textarea, actions); $('prompt-editors').appendChild(section);
      }
    } catch (e) { if (requestId === promptState.loading) promptMessage(e.message, true); }
  }
  document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
    if (button.dataset.tab === 'config' && !$('config-panel').hidden) return;
    if (button.dataset.tab !== 'config' && !$('config-panel').hidden && promptsDirty() && !window.confirm('提示词有未保存的修改，确定离开吗？')) return;
    if (button.dataset.tab !== 'config' && !$('config-panel').hidden) { promptState.entries = {}; promptState.loading++; }
    document.querySelectorAll('[data-tab]').forEach(other => other.setAttribute('aria-selected', String(other === button)));
    $('translate-panel').hidden = button.dataset.tab !== 'translate';
    if ($('knowledge-panel')) $('knowledge-panel').hidden = button.dataset.tab !== 'knowledge';
    $('config-panel').hidden = button.dataset.tab !== 'config';
    if (button.dataset.tab === 'config') loadPrompts();
    if (button.dataset.tab === 'knowledge') library.open();
  }));
  let promptLanguage = $('language').value;
  $('language').addEventListener('change', event => {
    if (!$('config-panel').hidden && promptsDirty() && !window.confirm('提示词有未保存的修改，确定切换语言吗？')) {
      $('language').value = promptLanguage; event.stopImmediatePropagation(); return;
    }
    if (promptLanguage !== $('language').value) resourcePreloads.clear();
    promptLanguage = $('language').value;
    if (!$('config-panel').hidden) loadPrompts();
    library?.languageChanged();
    if (library) preloadResources(promptLanguage).catch(() => {});
  });
  window.addEventListener('beforeunload', event => { if (promptsDirty()) { event.preventDefault(); event.returnValue = ''; } });
  const inputButtons = [...document.querySelectorAll('[data-input]')];
  function selectInput(mode) {
    state.input = mode;
    for (const button of inputButtons) {
      const active = button.dataset.input === mode;
      button.classList.toggle('selected', active);
      button.setAttribute('aria-pressed', String(active));
    }
    $('file-input').hidden = mode !== 'file';
    $('manual-input').hidden = mode !== 'manual';
    syncTranslationControls();
  }
  inputButtons.forEach(button => button.addEventListener('click', () => selectInput(button.dataset.input)));
  $('add-row').addEventListener('click', manualGrid.addRow);
  $('copy-translations').addEventListener('click', () => copyManual(true));
  $('copy-table').addEventListener('click', () => copyManual(false));
  $('clear-manual').addEventListener('click', () => {
    if ($('start-job').disabled) return;
    if (manualGrid.hasContent() && !window.confirm('清空原文、上下文和译文？请先复制需要保留的内容。')) return;
    manualGrid.clear();
    state.submitted = null;
    state.submittedJob = null;
    if (state.jobInput === 'manual') {
      state.activeJobId = null;
      state.retryJob = null;
      state.stopJob = null;
      state.pollGeneration = (state.pollGeneration || 0) + 1;
      clearTimeout(state.timer);
      $('job-status').hidden = true;
      state.jobInput = null;
      clearResult();
    }
    clearMessages();
  });
  $('batch-mode').addEventListener('change', () => { $('batch-size-label').hidden = $('batch-mode').value !== 'manual'; });
  function setFileStatus(message = '', error = false) {
    const status = $('file-status');
    status.hidden = !message;
    status.classList.toggle('file-error', error);
    status.textContent = message;
  }

  function forgetUpload(id) {
    if (id) request(`/api/uploads/${encodeURIComponent(id)}`, {method:'DELETE', keepalive:true}).catch(() => {});
  }

  function fileDescription(file) {
    const type = file.name.match(/\.(csv|xlsx)$/i)?.[1]?.toUpperCase() || '文件';
    const inKilobytes = file.size >= 1024;
    const size = inKilobytes ? file.size / 1024 : file.size;
    return `${type} · ${size.toLocaleString('zh-CN', {maximumFractionDigits:1})} ${inKilobytes ? 'KB' : 'B'}`;
  }

  function clearFileSelection() {
    state.uploadSequence++;
    state.uploading = false;
    forgetUpload(state.upload?.upload_id);
    state.upload = null;
    $('sheet-config').hidden = true;
    $('sheet-picker').replaceChildren();
    $('sheet-list').replaceChildren();
    setFileStatus();
    if (state.jobInput === 'file') {
      state.pollGeneration = (state.pollGeneration || 0) + 1;
      clearTimeout(state.timer);
      state.activeJobId = state.retryJob = state.stopJob = state.jobInput = null;
      $('stop-job').hidden = true;
      clearResult();
    }
    const file = $('file').files[0];
    $('translation-file-picker').dataset.selected = String(Boolean(file));
    $('upload-name').textContent = file?.name || '待翻译文件';
    $('upload-info').textContent = file ? fileDescription(file) : '支持 CSV 和 XLSX';
    $('file-action').textContent = file ? '更换文件' : '选择文件';
    syncTranslationControls();
  }

  function sheetPicker(container, sheets, current, activate, label) {
    container.replaceChildren();
    if (sheets.length < 2) return;
    if (sheets.length > 6) {
      const select = node('select');
      select.setAttribute('aria-label', label);
      selectOptions(select, sheets.map(sheet => sheet.name));
      select.value = current.name;
      select.addEventListener('change', () => activate(sheets[select.selectedIndex]));
      container.appendChild(field(label, select));
      return;
    }
    const group = node('div', null, 'sheet-tabs');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', label);
    for (const sheet of sheets) {
      const button = node('button', sheet.name, 'text-button compact');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(sheet === current));
      button.addEventListener('click', () => {
        for (const other of group.children) other.setAttribute('aria-pressed', String(other === button));
        activate(sheet);
      });
      group.appendChild(button);
    }
    container.appendChild(group);
  }

  function sheetTable(sheet) {
    const preview = node('table');
    preview.setAttribute('aria-label', sheet.name === 'CSV_Sheet' ? 'CSV 文件预览' : `${sheet.name} 工作表预览`);
    table(preview, sheet.columns, sheet.rows || []);
    const scroll = node('div', null, 'table-scroll sheet-table-scroll');
    scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', preview.getAttribute('aria-label'));
    scroll.appendChild(preview);
    return scroll;
  }

  function mappingProblem(sheets) {
    if (!sheets.length) return '请至少勾选一张工作表参与翻译';
    for (const sheet of sheets) {
      const prefix = sheets.length === 1 && sheet.name === 'CSV_Sheet' ? '' : `「${sheet.name}」`;
      if (!sheet.source) return `${prefix}请选择原文列`;
      if (sheet.context === sheet.source) return `${prefix}上下文列不能与原文列相同`;
      if (sheet.targetColumn) {
        if ([sheet.source, sheet.context].includes(sheet.targetColumn)) return `${prefix}目标列不能与原文列或上下文列相同`;
      } else if (!sheet.newName.trim()) return `${prefix}请填写新列名称`;
      else if (sheet.columns.includes(sheet.newName.trim())) return `${prefix}新列名称已存在，请改名或选择写入已有列`;
    }
    return '';
  }

  function renderMapping(sheet) {
    state.activeSheet = sheet;
    const section = node('div', null, 'sheet-card');
    const include = node('input');
    include.type = 'checkbox';
    include.checked = sheet.included;
    include.disabled = sheet.row_count === 0;
    include.addEventListener('change', () => {
      sheet.included = include.checked;
      syncTranslationControls();
    });
    const heading = node('div', null, 'sheet-title');
    const inclusion = node('label');
    inclusion.append(include, node('span', '参与翻译'));
    heading.append(inclusion, node('span', `${sheet.row_count} 行`, 'muted small'));
    section.appendChild(heading);

    const fields = node('div', null, 'sheet-fields');
    const source = node('select');
    selectOptions(source, sheet.columns);
    source.add(new Option('选择原文列', ''), 0);
    source.value = sheet.source;
    const context = node('select');
    selectOptions(context, sheet.columns, true);
    context.value = sheet.context;
    const target = node('select');
    selectOptions(target, sheet.columns);
    target.add(new Option('新建译文列', ''), 0);
    target.value = sheet.targetColumn;
    const targetName = node('input');
    targetName.className = 'target-name';
    targetName.value = sheet.newName;
    const nameField = field('新列名称', targetName);
    nameField.hidden = Boolean(sheet.targetColumn);
    const existing = node('p', '会覆盖下载文件中该列的对应单元格', 'muted small');
    existing.hidden = !sheet.targetColumn;
    const targetGroup = node('div', null, 'sheet-target-group');
    targetGroup.append(field('目标列', target), nameField, existing);
    for (const [control, key] of [[source, 'source'], [context, 'context'], [target, 'targetColumn'], [targetName, 'newName']]) {
      control.addEventListener(control === targetName ? 'input' : 'change', () => {
        sheet[key] = control.value;
        nameField.hidden = Boolean(sheet.targetColumn);
        existing.hidden = !sheet.targetColumn;
        syncTranslationControls();
      });
    }
    fields.append(field('原文列', source), field('上下文列（可选）', context), targetGroup);
    section.append(fields, sheetTable(sheet));
    $('sheet-list').replaceChildren(section);
  }

  function guessColumn(columns, aliases) {
    const matches = columns.filter(column => aliases.includes(column.toLowerCase()));
    return matches.length === 1 ? matches[0] : '';
  }

  function showSheets(upload) {
    for (const sheet of upload.sheets) {
      sheet.included = sheet.row_count > 0;
      sheet.source = guessColumn(sheet.columns, ['原文', '原文术语', '源文本', '中文', '简体中文', 'chinese', 'source', 'source_text', 'text']);
      sheet.context = guessColumn(sheet.columns, ['上下文', 'context', 'text_type', 'type', 'description']);
      sheet.targetColumn = '';
      sheet.newName = $('language').selectedOptions[0]?.textContent || '翻译';
    }
    const first = upload.sheets.find(sheet => sheet.included) || upload.sheets[0];
    sheetPicker($('sheet-picker'), upload.sheets, first, renderMapping, '查看工作表');
    renderMapping(first);
    $('sheet-config').hidden = false;
  }

  $('file').addEventListener('change', clearFileSelection);
  $('remove-file').addEventListener('click', () => {
    $('file').value = '';
    clearFileSelection();
    $('file').focus();
  });
  $('preview-file').addEventListener('click', async () => {
    const file = $('file').files[0];
    if (!file || state.uploading || state.busy) return;
    const sequence = ++state.uploadSequence;
    const body = new FormData();
    body.append('file', file);
    state.uploading = true;
    setFileStatus('正在上传并读取文件…');
    syncTranslationControls();
    try {
      const upload = await request('/api/uploads/preview', {method:'POST', body});
      if (sequence !== state.uploadSequence) { forgetUpload(upload.upload_id); return; }
      state.upload = upload;
      showSheets(upload);
      setFileStatus('文件已读取，请确认工作表与列配置');
    } catch (error) {
      if (sequence === state.uploadSequence) setFileStatus(error.message, true);
    } finally {
      if (sequence === state.uploadSequence) {
        state.uploading = false;
        syncTranslationControls();
      }
    }
  });
  function jobPayload() {
    const payload = { project_id: projectId, language: $('language').value, model: $('model').value, batch_size: Number($('batch-size').value), use_smart_budget: $('batch-mode').value === 'auto', custom_instructions: $('instructions').value, use_exact_memory: $('exact-memory').checked };
    if (state.input === 'manual') {
      state.submitted = manualGrid.snapshot();
      payload.manual_rows = state.submitted.map(({source_text, context, translated_text}) => ({source_text, context, translated_text}));
      payload.manual_target = $('language').selectedOptions[0]?.textContent || '翻译';
      if (!payload.manual_rows.some(row => row.source_text.trim())) throw new Error('请至少输入一行原文。');
    } else {
      if (!state.upload) throw new Error('请先上传并预览文件。');
      const selected = state.upload.sheets.filter(sheet => sheet.included);
      const problem = mappingProblem(selected);
      if (problem) throw new Error(problem);
      payload.upload_id = state.upload.upload_id;
      payload.sheets = selected.map(sheet => ({
        name: sheet.name, source: sheet.source, context: sheet.context || null,
        target: sheet.targetColumn || sheet.newName.trim(),
        target_mode: sheet.targetColumn ? 'existing' : 'new',
      }));
    }
    return payload;
  }
  function syncTranslationControls() {
    const fileMode = state.input === 'file';
    const hasFile = Boolean($('file').files.length);
    const sheets = state.upload?.sheets || [];
    const selected = sheets.filter(sheet => sheet.included);
    const problem = state.upload ? mappingProblem(selected) : '';
    $('preview-file').hidden = Boolean(state.upload);
    $('preview-file').disabled = !hasFile || state.uploading || state.busy;
    $('preview-file').textContent = state.uploading ? '正在读取…' : '上传并预览';
    $('remove-file').hidden = !hasFile;
    $('remove-file').disabled = state.busy;
    $('file').disabled = state.busy;
    $('sheet-count').textContent = state.upload ? `已选 ${selected.length} / ${sheets.length} 张` : '';
    $('sheet-problem').textContent = problem;
    $('sheet-problem').hidden = !problem;
    $('translation-footer').hidden = fileMode && !state.upload;
    $('start-job').disabled = state.busy || (fileMode && (!state.upload || Boolean(problem)));
    $('job-status').hidden = state.jobInput !== state.input;
    $('result-panel').hidden = state.resultInput !== state.input;
  }

  function setJobBusy(busy) {
    state.busy = busy;
    for (const button of inputButtons) button.disabled = busy;
    $('clear-manual').disabled = busy;
    syncTranslationControls();
  }

  function clearResult() {
    state.resultInput = state.resultJob = null;
    $('result-file').hidden = true;
    $('result-file').textContent = '';
    $('download').hidden = true;
    $('download').removeAttribute('href');
    $('retry-result').hidden = true;
    $('result-note').textContent = '';
    $('result-sheet-picker').replaceChildren();
    $('result-sheets').replaceChildren();
    syncTranslationControls();
  }
  function presentJob(job) {
    state.jobInput = job.input_mode;
    setJobBusy(['queued', 'running', 'cancelling'].includes(job.status));
    state.stopJob = job.can_cancel ? job.id : null;
    $('stop-job').hidden = !['queued', 'running', 'cancelling'].includes(job.status);
    $('stop-job').disabled = Boolean(state.stopPending) || !state.stopJob;
    $('stop-job').textContent = job.status === 'cancelling' ? '正在停止…' : '停止翻译';
    const statuses = {queued:'排队中',running:'正在翻译',complete:'翻译完成',completed:'翻译完成',partial:'翻译部分完成',failed:'翻译失败',interrupted:'任务中断',cancelling:'正在停止翻译…',cancelled:'翻译已停止'};
    $('job-message').textContent = job.message || statuses[job.status] || job.status;
    const waitingForWorker = job.worker_online === false && ['queued', 'running', 'cancelling'].includes(job.status);
    workerHint.hidden = !waitingForWorker;
    if (waitingForWorker) workerHint.textContent = job.status === 'queued'
      ? '后台处理服务未运行，任务正在等待。请启动后台处理服务。'
      : '后台处理服务可能已停止，正在确认任务状态。请检查后台处理服务。';
    const completed = Number(job.completed || 0), total = Number(job.total || 0);
    $('job-count').textContent = total ? `${completed} / ${total} 行` : '';
    $('job-progress').value = ['complete', 'completed'].includes(job.status) ? 100 : (total ? Math.min(99, completed / total * 100) : 0);
    state.retryJob = job.can_retry ? job.id : null;
    $('retry-job').hidden = !state.retryJob;
    $('retry-note').hidden = !state.retryJob;
    const manualDownload = $('manual-download');
    manualDownload.hidden = job.input_mode !== 'manual' || !job.download_url;
    if (!manualDownload.hidden) manualDownload.href = job.download_url;
    if (job.input_mode === 'file' && job.status === 'completed') {
      state.resultInput = 'file';
      state.resultJob = job;
      $('result-file').textContent = job.filename || '';
      $('result-file').hidden = !job.filename;
      $('download').hidden = !job.download_url;
      if (job.download_url) $('download').href = job.download_url;
      syncTranslationControls();
    }
    if (job.error) showError(job.error);
  }
  async function loadResult(id, job, generation = state.pollGeneration) {
    if (job.input_mode === 'manual') {
      const result = await request(`/api/jobs/${encodeURIComponent(id)}/manual-result`);
      if (state.activeJobId !== id || generation !== state.pollGeneration) return;
      const rows = result.rows || [];
      let skipped = 0;
      const mismatch = job.language !== $('language').value;
      const available = [...$('language').options].some(option => option.value === job.language);
      const restoreLanguage = mismatch && state.submittedJob !== id && !state.languageChanged && available;
      const languageConflict = mismatch && !restoreLanguage;
      if (restoreLanguage) $('language').value = job.language;
      if (languageConflict) skipped = rows.length;
      else if (state.submittedJob === id && state.submitted) {
        skipped = manualGrid.applyResults(state.submitted, rows);
      } else if (!manualGrid.changed && !manualGrid.hasContent()) {
        manualGrid.replaceRows(rows);
        selectInput('manual');
      } else skipped = rows.length;
      if (skipped) {
        showNotice(languageConflict ? '目标语言已更改，未将旧语言的译文填入表格。可查看下方全部原始结果。' : `有 ${skipped} 行在翻译期间被修改或删除，未覆盖这些单元格。可查看下方全部原始结果。`);
        const container = $('result-sheets'); container.replaceChildren();
        const original = node('table'); table(original, ['原文', '上下文', '译文'], rows.map(item => [item.source_text, item.context, item.translated_text]));
        const scroll = node('div', null, 'table-scroll'); scroll.appendChild(original); container.appendChild(scroll);
        $('result-note').textContent = '以下为翻译时的全部原始结果，不含表格中的后续修改。';
        state.resultInput = 'manual';
        syncTranslationControls();
        return;
      }
      clearResult();
      if (state.input !== 'manual') showNotice('译文已填入手动输入表格。');
      return;
    }
    const current = () => state.activeJobId === id && generation === state.pollGeneration;
    $('retry-result').hidden = true;
    $('result-note').textContent = '正在加载结果预览…';
    try {
      const result = await request(`/api/jobs/${encodeURIComponent(id)}/preview`);
      if (!current()) return;
      const selected = new Set(job.selected_sheets || []);
      const sheets = result.sheets || [];
      const first = sheets.find(sheet => selected.has(sheet.name)) || sheets[0];
      if (!first) {
        $('result-note').textContent = '文件已生成，可直接下载完整结果';
        return;
      }
      const render = sheet => {
        const status = selected.size ? (selected.has(sheet.name) ? '本次选中' : '原样保留') : '';
        const count = `显示 ${sheet.rows.length} / ${sheet.row_count} 行`;
        $('result-note').textContent = status ? `${status} · ${count}` : count;
        $('result-sheets').replaceChildren(sheetTable(sheet));
      };
      sheetPicker($('result-sheet-picker'), sheets, first, render, '查看结果工作表');
      render(first);
    } catch (error) {
      if (!current()) return;
      $('result-note').textContent = `结果预览加载失败：${error.message}`;
      $('retry-result').hidden = false;
    }
  }
  $('retry-result').addEventListener('click', () => {
    const job = state.resultJob;
    if (job) loadResult(job.id, job);
  });
  async function poll(id, generation = state.pollGeneration) {
    try { const job = await request(`/api/jobs/${encodeURIComponent(id)}`); if (state.activeJobId !== id || generation !== state.pollGeneration) return; presentJob(job); if (['queued','running','cancelling'].includes(job.status)) state.timer = setTimeout(() => poll(id, generation), 2000); else if (job.status === 'completed') await loadResult(id, job, generation); }
    catch (e) { if (state.activeJobId !== id || generation !== state.pollGeneration) return; setJobBusy(false); showError(e.message); }
  }
  $('start-job').addEventListener('click', async () => {
    if (state.busy) return;
    clearMessages();
    try {
      const payload = jobPayload();
      setJobBusy(true);
      if (!isDirect) await preloadResources(payload.language);
      const job = await request('/api/jobs', {method:'POST',body:payload});
      state.activeJobId = job.id;
      state.pollGeneration = (state.pollGeneration || 0) + 1;
      state.submittedJob = payload.manual_rows ? job.id : null;
      clearResult();
      presentJob(job);
      clearTimeout(state.timer);
      poll(job.id);
    } catch (error) {
      setJobBusy(false);
      showError(error.message);
    }
  });
  $('stop-job').addEventListener('click', async () => {
    if (!state.stopJob || state.stopPending) return;
    const id = state.stopJob, generation = state.pollGeneration;
    state.stopPending = true; $('stop-job').disabled = true;
    try {
      const job = await request(`/api/jobs/${encodeURIComponent(id)}/cancel`, {method: 'POST'});
      if (generation === state.pollGeneration) presentJob(job);
    } catch (e) { showError(e.message); }
    finally { state.stopPending = false; $('stop-job').disabled = !state.stopJob; }
  });
  $('retry-job').addEventListener('click', async () => {
    if (!state.retryJob || state.busy) return;
    clearMessages(); $('retry-job').disabled = true; setJobBusy(true);
    try {
      const job = await request(`/api/jobs/${encodeURIComponent(state.retryJob)}/retry`, {method: 'POST'});
      state.activeJobId = job.id;
      state.pollGeneration = (state.pollGeneration || 0) + 1;
      clearResult();
      presentJob(job);
      clearTimeout(state.timer);
      poll(job.id);
    } catch (e) { setJobBusy(false); showError(e.message); }
    finally { $('retry-job').disabled = false; }
  });
  async function clearPreviousJobs() {
    setJobBusy(true);
    try {
      try { sessionStorage.removeItem(`locatool-job-${projectId}`); } catch (_) { /* Storage may be disabled. */ }
      await request('/api/jobs/clear', {method:'POST', body:{project_id:projectId}});
      setJobBusy(false);
    } catch (e) { setJobBusy(false); showError(e.message); }
  }
  selectInput('manual');
  clearPreviousJobs();
  window.addEventListener('pagehide', () => {
    resourcePreloads.clear();
    state.uploadSequence++;
    forgetUpload(state.upload?.upload_id);
    fetch('/api/jobs/clear', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({project_id: projectId})
    }).catch(() => {});
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    $('file').value = '';
    clearFileSelection();
    setJobBusy(false);
  });
  let previousLanguageName = $('language').selectedOptions[0]?.textContent || '翻译';
  $('language').addEventListener('change', () => {
    state.languageChanged = true;
    if (manualGrid.hasTranslations()) {
      showNotice('目标语言已更改；表格中的现有译文不会自动改变。');
    }
    const current = $('language').selectedOptions[0]?.textContent || '翻译';
    for (const sheet of state.upload?.sheets || []) {
      if (sheet.newName === previousLanguageName) sheet.newName = current;
    }
    const name = $('sheet-list').querySelector('.target-name');
    if (name) name.value = state.activeSheet.newName;
    syncTranslationControls();
    previousLanguageName = current;
  });
  if (isDirect) return;
  preloadResources($('language').value).catch(() => {});
})();
