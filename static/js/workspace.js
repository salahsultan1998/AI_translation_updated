(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const workspace = $('workspace');
  const projectId = workspace.dataset.project;
  const isDirect = workspace.dataset.direct === 'true';
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const state = { input: 'file', upload: null, knowledge: null, cleanPairs: [], conflicts: [], timer: null, manualChanged: false, languageChanged: false, submitted: null };
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; };
  const workerHint = node('p', '', 'muted small');
  workerHint.setAttribute('role', 'status');
  workerHint.hidden = true;
  $('job-status').appendChild(workerHint);
  const resourcePreloads = new Map();
  const knowledgeStatus = node('p', '', 'muted small');
  knowledgeStatus.setAttribute('role', 'status');
  if (!isDirect) $('language').closest('section').insertAdjacentElement('afterend', knowledgeStatus);
  function preloadResources(language) {
    const existing = resourcePreloads.get(language);
    if (existing && existing.expires > Date.now()) {
      if (existing.result && $('language').value === language) showKnowledgeStatus(existing.result);
      return existing.promise;
    }
    const entry = {expires: Infinity};
    resourcePreloads.set(language, entry);
    if ($('language').value === language) knowledgeStatus.textContent = '正在加载项目知识库…';
    entry.promise = request(`/api/projects/${encodeURIComponent(projectId)}/knowledge/preload`, {
      method: 'POST', body: {language}
    }).then(result => {
      entry.expires = Date.now() + result.expires_in * 1000;
      entry.result = result;
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
  function refreshKnowledge() {
    resourcePreloads.clear();
    preloadResources($('language').value).catch(() => {});
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
    if (!response.ok) throw new Error(data.error || '操作失败，请稍后重试。');
    return data;
  }
  function table(target, columns, rows) {
    target.replaceChildren();
    const head = target.createTHead().insertRow();
    columns.forEach(col => head.appendChild(node('th', col)));
    const body = target.createTBody();
    rows.forEach(row => { const tr = body.insertRow(); columns.forEach((col, index) => { const value = Array.isArray(row) ? row[index] : row[col]; tr.appendChild(node('td', value == null ? '' : String(value))); }); });
  }
  function selectOptions(select, values, includeBlank = false) {
    select.replaceChildren();
    if (includeBlank) { const blank = node('option', '无'); blank.value = ''; select.appendChild(blank); }
    values.forEach(value => { const option = node('option', value); option.value = value; select.appendChild(option); });
  }
  function field(label, input) { const wrap = node('label', label); wrap.appendChild(input); return wrap; }
  let nextManualId = 0;
  const manualRows = () => [...$('manual-rows').querySelectorAll('.manual-row')];
  const manualClasses = ['manual-source', 'manual-context', 'manual-translation'];
  function renumberManualRows() {
    manualRows().forEach((row, i) => { row.querySelector('.manual-number').textContent = String(i + 1); });
  }
  function addManualRow(source = '', context = '', translated = '', renumber = true) {
    const row = node('tr', null, 'manual-row'); row.dataset.rowId = String(++nextManualId);
    const number = node('th', null, 'manual-number'); number.scope = 'row'; row.appendChild(number);
    [source, context, translated].forEach((value, index) => {
      const cell = node('td'); const input = node('textarea'); input.rows = 2; input.value = value;
      input.className = manualClasses[index]; input.setAttribute('aria-label', ['原文', '上下文', '译文'][index]); input.setAttribute('dir', 'auto');
      input.addEventListener('input', () => { state.manualChanged = true; });
      input.addEventListener('paste', event => pasteCells(event, row, index));
      cell.appendChild(input); row.appendChild(cell);
    });
    const actions = node('td'); const remove = node('button', '删除', 'secondary compact'); remove.type = 'button';
    remove.addEventListener('click', () => { row.remove(); state.manualChanged = true; if (!manualRows().length) addManualRow(); renumberManualRows(); });
    actions.appendChild(remove); row.appendChild(actions); $('manual-rows').appendChild(row); if (renumber) renumberManualRows(); return row;
  }
  // Excel's text clipboard uses TSV with CSV-style quotes for embedded tabs/newlines.
  function parseClipboard(text) {
    const rows = []; let row = [], value = '', quoted = false, endedWithNewline = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { value += '"'; i++; }
        else if (ch === '"') quoted = false;
        else value += ch;
      } else if (ch === '"' && value === '') { quoted = true; endedWithNewline = false; }
      else if (ch === '\t') { row.push(value); value = ''; endedWithNewline = false; }
      else if (ch === '\r' || ch === '\n') {
        if (ch === '\r' && text[i + 1] === '\n') i++;
        row.push(value); rows.push(row); row = []; value = ''; endedWithNewline = true;
      } else { value += ch; endedWithNewline = false; }
    }
    if (!endedWithNewline || row.length || value) { row.push(value); rows.push(row); }
    return rows;
  }
  function pasteCells(event, anchor, startColumn) {
    const text = event.clipboardData?.getData('text/plain');
    if (!text || (!text.includes('\t') && !text.includes('\n') && !text.includes('\r'))) return;
    const values = parseClipboard(text);
    if (values.length === 1 && values[0].length === 1) {
      event.preventDefault();
      const input = anchor.querySelector('.' + manualClasses[startColumn]);
      input.setRangeText(values[0][0], input.selectionStart, input.selectionEnd, 'end');
      state.manualChanged = true; return;
    }
    event.preventDefault();
    const current = manualRows(); const start = current.indexOf(anchor);
    if (start + values.length > 10000) { showError('手动输入最多支持 10000 行。'); return; }
    let truncated = false;
    values.forEach((cells, offset) => {
      let row = current[start + offset];
      if (!row) { row = addManualRow('', '', '', false); current.push(row); }
      cells.forEach((value, col) => {
        if (startColumn + col >= manualClasses.length) { truncated = true; return; }
        row.querySelector('.' + manualClasses[startColumn + col]).value = value;
      });
    });
    renumberManualRows(); state.manualChanged = true;
    if (truncated) showNotice('已粘贴表格内的列，超出原文、上下文和译文的列未粘贴。');
  }
  function tsvCell(value) { return /[\t\r\n"]/.test(value) ? '"' + value.replace(/"/g, '""') + '"' : value; }
  async function copyManual(columns) {
    const text = manualRows().map(row => columns.map(cls => tsvCell(row.querySelector('.' + cls).value)).join('\t')).join('\r\n');
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
    showNotice(columns.length === 1 ? '已复制全部译文，可粘贴到 Excel。' : '已复制整表，可粘贴到 Excel。');
  }
  function addTermRow() {
    const row = node('div', null, 'term-row');
    for (const [title, cls] of [['原文', 'term-source'], ['译文', 'term-target'], ['备注（可选）', 'term-note']]) { const input = node('input'); input.className = cls; row.appendChild(field(title, input)); }
    const remove = node('button', '删除', 'secondary compact'); remove.type = 'button'; remove.addEventListener('click', () => row.remove()); row.appendChild(remove); $('term-rows').appendChild(row);
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
        const heading = node('h3', labels[kind][0]); const description = node('p', `${labels[kind][1]} · 保存到 ${data.path}`, 'muted small');
        const textarea = node('textarea'); textarea.value = data.content; textarea.setAttribute('aria-label', labels[kind][0]); textarea.dir = 'auto';
        const actions = node('div', null, 'prompt-actions'); const status = node('span', '', 'muted small');
        const discard = node('button', '放弃修改', 'secondary compact'); discard.type = 'button'; discard.disabled = true;
        const save = node('button', '保存', 'compact'); save.type = 'button'; save.disabled = true;
        const entry = {textarea, saved: data.content, version: data.version}; promptState.entries[kind] = entry;
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
    document.querySelectorAll('[data-tab]').forEach(other => { const active = other === button; other.classList.toggle('active', active); other.setAttribute('aria-selected', String(active)); });
    $('translate-panel').hidden = button.dataset.tab !== 'translate';
    if ($('knowledge-panel')) $('knowledge-panel').hidden = button.dataset.tab !== 'knowledge';
    $('config-panel').hidden = button.dataset.tab !== 'config';
    if (button.dataset.tab === 'config') loadPrompts();
  }));
  let promptLanguage = $('language').value;
  $('language').addEventListener('change', event => {
    if (!$('config-panel').hidden && promptsDirty() && !window.confirm('提示词有未保存的修改，确定切换语言吗？')) {
      $('language').value = promptLanguage; event.stopImmediatePropagation(); return;
    }
    promptLanguage = $('language').value;
    if (!$('config-panel').hidden) loadPrompts();
  });
  window.addEventListener('beforeunload', event => { if (promptsDirty()) { event.preventDefault(); event.returnValue = ''; } });
  document.querySelectorAll('[data-input]').forEach(button => button.addEventListener('click', () => {
    state.input = button.dataset.input; document.querySelectorAll('[data-input]').forEach(other => other.classList.toggle('selected', other === button));
    $('file-input').hidden = state.input !== 'file'; $('manual-input').hidden = state.input !== 'manual';
  }));
  $('add-row').addEventListener('click', () => { addManualRow(); state.manualChanged = true; }); addManualRow();
  $('copy-translations').addEventListener('click', () => copyManual(['manual-translation']));
  $('copy-table').addEventListener('click', () => copyManual(manualClasses));
  $('batch-mode').addEventListener('change', () => { $('batch-size-label').hidden = $('batch-mode').value !== 'manual'; });
  $('file').addEventListener('change', () => { state.upload = null; $('sheet-config').hidden = true; $('upload-name').textContent = $('file').files[0]?.name || '点击选择文件'; });
  async function previewUpload(input) { if (!input.files[0]) throw new Error('请先选择文件。'); const body = new FormData(); body.append('file', input.files[0]); return request('/api/uploads/preview', { method: 'POST', body }); }
  function showSheets(data) {
    const container = $('sheet-list'); container.replaceChildren();
    data.sheets.forEach(sheet => {
      const card = node('div', null, 'sheet-card'); card.dataset.name = sheet.name;
      const title = node('div', null, 'sheet-title'); const selectSheet = node('input'); selectSheet.type = 'checkbox'; selectSheet.checked = true; selectSheet.className = 'sheet-enabled'; const sheetLabel = node('label'); sheetLabel.append(selectSheet, node('span', `${sheet.name}（${sheet.row_count} 行）`)); title.appendChild(sheetLabel); card.appendChild(title);
      const fields = node('div', null, 'fields three');
      const source = node('select'); source.className = 'source'; selectOptions(source, sheet.columns);
      const context = node('select'); context.className = 'context'; selectOptions(context, sheet.columns, true); const contextGuess = sheet.columns.find(c => ['context','text_type','type','description'].includes(c.toLowerCase())); if (contextGuess) context.value = contextGuess;
      const target = node('select'); target.className = 'target'; const create = node('option', '新建列'); create.value = ''; target.appendChild(create); sheet.columns.forEach(c => { const option = node('option', c); option.value = c; target.appendChild(option); });
      const targetName = node('input'); targetName.className = 'target-name'; targetName.placeholder = '新列名称'; targetName.value = $('language').selectedOptions[0]?.textContent || '翻译';
      target.addEventListener('change', () => { targetName.disabled = Boolean(target.value); });
      const targetWrap = field('目标列', target); targetWrap.appendChild(targetName);
      fields.append(field('原文列', source), field('上下文列（可选）', context), targetWrap); card.appendChild(fields);
      const preview = node('table'); table(preview, sheet.columns, sheet.rows || []); const scroll = node('div', null, 'table-scroll'); scroll.appendChild(preview); card.appendChild(scroll); container.appendChild(card);
    });
    $('sheet-config').hidden = false;
  }
  $('preview-file').addEventListener('click', async () => { clearMessages(); try { state.upload = await previewUpload($('file')); showSheets(state.upload); } catch (e) { showError(e.message); } });
  function jobPayload() {
    const payload = { project_id: projectId, language: $('language').value, model: $('model').value, batch_size: Number($('batch-size').value), use_smart_budget: $('batch-mode').value === 'auto', use_exact_memory: $('exact-memory').checked };
    if (state.input === 'manual') {
      state.submitted = manualRows().map(row => ({ id: row.dataset.rowId, source_text: row.querySelector('.manual-source').value, context: row.querySelector('.manual-context').value, target: row.querySelector('.manual-translation').value }));
      payload.manual_rows = state.submitted.map(({source_text, context, target}) => ({source_text, context, translated_text: target}));
      payload.manual_target = $('language').selectedOptions[0]?.textContent || '翻译';
      if (!payload.manual_rows.some(row => row.source_text.trim())) throw new Error('请至少输入一行原文。');
    } else {
      if (!state.upload) throw new Error('请先上传并预览文件。');
      payload.upload_id = state.upload.upload_id;
      payload.sheets = [...document.querySelectorAll('.sheet-card')].filter(card => card.querySelector('.sheet-enabled').checked).map(card => ({ name: card.dataset.name, source: card.querySelector('.source').value, context: card.querySelector('.context').value || null, target: card.querySelector('.target').value || card.querySelector('.target-name').value.trim() }));
      if (!payload.sheets.length) throw new Error('请至少选择一个工作表。');
      if (payload.sheets.some(sheet => !sheet.source || !sheet.target)) throw new Error('请为每个工作表选择原文列并填写目标列。');
    }
    return payload;
  }
  function presentJob(job) {
    $('job-status').hidden = false;
    $('start-job').disabled = ['queued', 'running'].includes(job.status);
    const statuses = {queued:'排队中',running:'正在翻译',complete:'翻译完成',completed:'翻译完成',partial:'翻译部分完成',failed:'翻译失败',interrupted:'任务中断',cancelled:'任务已取消'};
    $('job-message').textContent = job.message || statuses[job.status] || job.status;
    const waitingForWorker = job.worker_online === false && ['queued', 'running'].includes(job.status);
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
    const download = $('download'); download.hidden = !job.download_url;
    download.textContent = job.input_mode === 'manual' ? '下载原始翻译文件（不含表格修改） ↓' : '下载翻译文件 ↓';
    if (job.download_url) download.href = job.download_url;
    if (job.error) showError(job.error);
  }
  async function loadResult(id, job) {
    if (job.input_mode === 'manual') {
      const result = await request(`/api/jobs/${encodeURIComponent(id)}/manual-result`);
      const rows = result.rows || [];
      let skipped = 0;
      const mismatch = job.language !== $('language').value;
      const available = [...$('language').options].some(option => option.value === job.language);
      const restoreLanguage = mismatch && state.submittedJob !== id && !state.languageChanged && available;
      const languageConflict = mismatch && !restoreLanguage;
      if (restoreLanguage) $('language').value = job.language;
      if (languageConflict) skipped = rows.length;
      else if (state.submittedJob === id && state.submitted) {
        const currentById = new Map(manualRows().map(row => [row.dataset.rowId, row]));
        rows.forEach((item, index) => {
          const snapshot = state.submitted[index];
          const row = snapshot && currentById.get(snapshot.id);
          if (!row || row.querySelector('.manual-source').value !== snapshot.source_text ||
              row.querySelector('.manual-context').value !== snapshot.context ||
              row.querySelector('.manual-translation').value !== snapshot.target) { skipped++; return; }
          row.querySelector('.manual-translation').value = item.translated_text;
        });
      } else if (!state.manualChanged && manualRows().every(row => manualClasses.every(cls => !row.querySelector('.' + cls).value))) {
        $('manual-rows').replaceChildren();
        rows.forEach(item => addManualRow(item.source_text, item.context, item.translated_text, false));
        if (!rows.length) addManualRow();
        else renumberManualRows();
        state.input = 'manual'; $('manual-input').hidden = false; $('file-input').hidden = true;
        document.querySelectorAll('[data-input]').forEach(button => button.classList.toggle('selected', button.dataset.input === 'manual'));
      } else skipped = rows.length;
      if (skipped) {
        showNotice(languageConflict ? '目标语言已更改，未将旧语言的译文填入表格。可查看下方全部原始结果。' : `有 ${skipped} 行在翻译期间被修改或删除，未覆盖这些单元格。可查看下方全部原始结果。`);
        const container = $('result-sheets'); container.replaceChildren();
        const original = node('table'); table(original, ['原文', '上下文', '译文'], rows.map(item => [item.source_text, item.context, item.translated_text]));
        const scroll = node('div', null, 'table-scroll'); scroll.appendChild(original); container.appendChild(scroll);
        $('result-note').textContent = '以下为翻译时的全部原始结果，不含表格中的后续修改。';
        $('result-panel').hidden = false; return;
      }
      else { $('result-panel').hidden = true; if (state.input !== 'manual') showNotice('译文已填入手动输入表格。'); return; }
    }
    const result = await request(`/api/jobs/${encodeURIComponent(id)}/preview`);
    const container = $('result-sheets'); container.replaceChildren();
    $('result-note').textContent = '每个工作表显示前十行；完整结果请下载文件。';
    for (const sheet of result.sheets || []) {
      const heading = node('h3', sheet.name); const preview = node('table');
      table(preview, sheet.columns, sheet.rows || []);
      const scroll = node('div', null, 'table-scroll'); scroll.appendChild(preview);
      const section = node('div', null, 'result-sheet'); section.append(heading, scroll); container.appendChild(section);
    }
    $('result-panel').hidden = !container.childElementCount;
  }
  async function poll(id) {
    try { const job = await request(`/api/jobs/${encodeURIComponent(id)}`); presentJob(job); if (['queued','running'].includes(job.status)) state.timer = setTimeout(() => poll(id), 2000); else { $('start-job').disabled = false; if (job.status === 'completed') await loadResult(id, job); } }
    catch (e) { $('start-job').disabled = false; showError(e.message); }
  }
  $('start-job').addEventListener('click', async () => {
    clearMessages(); try { const payload = jobPayload(); $('start-job').disabled = true; if (!isDirect) await preloadResources(payload.language); const job = await request('/api/jobs', {method:'POST',body:payload}); state.submittedJob = payload.manual_rows ? job.id : null; $('result-panel').hidden = true; presentJob(job); if (state.timer) clearTimeout(state.timer); poll(job.id); } catch (e) { $('start-job').disabled = false; showError(e.message); }
  });
  $('retry-job').addEventListener('click', async () => {
    if (!state.retryJob) return;
    clearMessages(); $('retry-job').disabled = true; $('start-job').disabled = true;
    try {
      const job = await request(`/api/jobs/${encodeURIComponent(state.retryJob)}/retry`, {method: 'POST'});
      presentJob(job); if (state.timer) clearTimeout(state.timer); poll(job.id);
    } catch (e) { $('start-job').disabled = false; showError(e.message); }
    finally { $('retry-job').disabled = false; }
  });
  async function clearPreviousJobs() {
    $('start-job').disabled = true;
    try {
      try { sessionStorage.removeItem(`locatool-job-${projectId}`); } catch (_) { /* Storage may be disabled. */ }
      await request('/api/jobs/clear', {method:'POST', body:{project_id:projectId}});
      $('start-job').disabled = false;
    } catch (e) { showError(e.message); }
  }
  clearPreviousJobs();
  window.addEventListener('pagehide', () => {
    fetch('/api/jobs/clear', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({project_id: projectId})
    }).catch(() => {});
  });
  let previousLanguageName = $('language').selectedOptions[0]?.textContent || '翻译';
  $('language').addEventListener('change', () => {
    state.languageChanged = true;
    if (manualRows().some(row => row.querySelector('.manual-translation').value)) {
      showNotice('目标语言已更改；表格中的现有译文不会自动改变。');
    }
    const current = $('language').selectedOptions[0]?.textContent || '翻译';
    document.querySelectorAll('.target-name').forEach(input => { if (input.value === previousLanguageName) input.value = current; });
    previousLanguageName = current;
  });
  if (isDirect) return;
  preloadResources($('language').value).catch(() => {});
  $('language').addEventListener('change', () => {
    knowledgeStatus.textContent = '正在加载项目知识库…';
    preloadResources($('language').value).catch(() => {});
  });
  $('add-term').addEventListener('click', addTermRow); addTermRow();
  function collectTerms() { return [...document.querySelectorAll('.term-row')].map(row => ({ source:row.querySelector('.term-source').value.trim(), target:row.querySelector('.term-target').value.trim(), note:row.querySelector('.term-note').value.trim() })).filter(pair => pair.source && pair.target); }
  async function saveTerms(pairs) { if (!pairs.length) { $('conflicts').hidden = true; showNotice('已保留所有现有术语，无需保存。'); return; } const result = await request(`/api/projects/${encodeURIComponent(projectId)}/glossary/save`, {method:'POST',body:{language:$('language').value,pairs}}); $('conflicts').hidden = true; showNotice(`已保存 ${result.count} 条术语。`); refreshKnowledge(); }
  $('check-terms').addEventListener('click', async () => { clearMessages(); try { const pairs = collectTerms(); if (!pairs.length) throw new Error('请至少填写一条完整的原文和译文。'); const result = await request(`/api/projects/${encodeURIComponent(projectId)}/glossary/check`, {method:'POST',body:{language:$('language').value,pairs}}); state.cleanPairs = result.clean_pairs || []; state.conflicts = result.conflicts || []; if (!state.conflicts.length) { await saveTerms(state.cleanPairs); return; } const list = $('conflict-list'); list.replaceChildren(); state.conflicts.forEach(conflict => { const card = node('div',null,'conflict-card'); card.append(node('strong',conflict.source),node('p',`已有：${conflict.existing_target} ${conflict.existing_note || ''}`),node('p',`新内容：${conflict.new_target} ${conflict.new_note || ''}`)); const overwrite = node('input'); overwrite.type = 'checkbox'; const label = node('label'); label.append(overwrite, node('span','用新内容覆盖')); card.appendChild(label); list.appendChild(card); }); $('conflicts').hidden = false; } catch (e) { showError(e.message); } });
  $('save-conflicts').addEventListener('click', async () => { try { const selected = [...$('conflict-list').children].flatMap((card, index) => card.querySelector('input').checked ? [{source:state.conflicts[index].source,target:state.conflicts[index].new_target,note:state.conflicts[index].new_note || ''}] : []); await saveTerms([...state.cleanPairs,...selected]); } catch (e) { showError(e.message); } });
  $('keep-all').addEventListener('click', () => document.querySelectorAll('#conflict-list input').forEach(input => { input.checked = false; }));
  $('overwrite-all').addEventListener('click', () => document.querySelectorAll('#conflict-list input').forEach(input => { input.checked = true; }));
  $('language').addEventListener('change', () => { $('conflicts').hidden = true; state.cleanPairs = []; state.conflicts = []; });
  $('knowledge-file').addEventListener('change', () => { state.knowledge = null; $('knowledge-mapping').hidden = true; });
  function updateKnowledgeSheet() { const sheet = state.knowledge.sheets.find(s => s.name === $('knowledge-sheet').value); if (!sheet) return; selectOptions($('knowledge-source'),sheet.columns); selectOptions($('knowledge-target'),sheet.columns); if (sheet.columns.length > 1) $('knowledge-target').selectedIndex = 1; selectOptions($('knowledge-note'),sheet.columns,true); table($('knowledge-preview'),sheet.columns,sheet.rows || []); }
  $('knowledge-sheet').addEventListener('change', updateKnowledgeSheet);
  $('preview-knowledge').addEventListener('click', async () => { clearMessages(); try { state.knowledge = await previewUpload($('knowledge-file')); selectOptions($('knowledge-sheet'), state.knowledge.sheets.map(sheet => sheet.name)); updateKnowledgeSheet(); $('knowledge-mapping').hidden = false; } catch (e) { showError(e.message); } });
  $('upload-knowledge').addEventListener('click', async () => { clearMessages(); try { if (!state.knowledge || !$('knowledge-file').files[0]) throw new Error('请先上传并预览文件。'); const body = new FormData(); body.append('file',$('knowledge-file').files[0]); for (const [key,id] of [['language','language'],['kind','knowledge-kind'],['sheet','knowledge-sheet'],['source','knowledge-source'],['target','knowledge-target'],['note','knowledge-note']]) body.append(key,$(id).value); const result = await request(`/api/projects/${encodeURIComponent(projectId)}/knowledge/upload`, {method:'POST',body}); showNotice(`已导入 ${result.count} 条记录。`); refreshKnowledge(); } catch (e) { showError(e.message); } });
})();
