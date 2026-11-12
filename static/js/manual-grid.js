/* The manual translation worksheet. Cells display text and share one editor. */
window.createManualGrid = function (body, {showError, showNotice}) {
  const maxRows = 10000;
  const labels = ['原文', '上下文', '译文'];
  const editor = document.createElement('textarea');
  const undo = [];
  let nextId = 0;
  let active, anchor, editing;
  let selected = [];
  let dragging = false;
  let changed = false;

  editor.className = 'manual-cell-editor';
  editor.dir = 'auto';

  const value = cell => cell.firstElementChild.textContent;
  const normalize = text => text.replace(/\r\n?/g, '\n');
  const write = (cell, text) => { cell.firstElementChild.textContent = normalize(text); };
  const cellAt = (row, column) => body.rows[row]?.cells[column + 1];
  const dataCells = row => Array.from(row.cells).slice(1, 4);

  function appendRow(values = ['', '', ''], parent = body) {
    const row = document.createElement('tr');
    row.className = 'manual-row';
    row.dataset.rowId = String(++nextId);
    const number = document.createElement('th');
    number.scope = 'row';
    number.className = 'manual-number';
    row.append(number);
    for (const text of values) {
      const cell = row.insertCell();
      cell.className = 'manual-cell';
      cell.tabIndex = -1;
      cell.setAttribute('aria-selected', 'false');
      const display = document.createElement('div');
      display.className = 'manual-value';
      display.dir = 'auto';
      display.textContent = normalize(text);
      cell.append(display);
    }
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.tabIndex = -1;
    remove.className = 'text-button manual-remove';
    remove.textContent = '删除';
    row.insertCell().append(remove);
    parent.append(row);
    return row;
  }

  function renumber(from = 0) {
    for (const [offset, row] of Array.from(body.rows).slice(from).entries()) {
      const i = from + offset;
      row.cells[0].textContent = i + 1;
      row.cells[4].firstElementChild.setAttribute('aria-label', `删除第 ${i + 1} 行`);
    }
  }

  function select(cell, extend = false, focus = true) {
    if (!cell) return;
    if (!extend || !anchor?.isConnected) anchor = cell;
    if (active) {
      active.tabIndex = -1;
      active.classList.remove('manual-active');
      active.parentNode.cells[4].firstElementChild.tabIndex = -1;
    }
    for (const previous of selected) previous.setAttribute('aria-selected', 'false');
    active = cell;
    active.tabIndex = 0;
    active.classList.add('manual-active');
    active.parentNode.cells[4].firstElementChild.tabIndex = 0;
    const rowStart = Math.min(anchor.parentNode.sectionRowIndex, cell.parentNode.sectionRowIndex);
    const rowEnd = Math.max(anchor.parentNode.sectionRowIndex, cell.parentNode.sectionRowIndex);
    const colStart = Math.min(anchor.cellIndex, cell.cellIndex) - 1;
    const colEnd = Math.max(anchor.cellIndex, cell.cellIndex) - 1;
    selected = [];
    for (let row = rowStart; row <= rowEnd; row++) {
      for (let col = colStart; col <= colEnd; col++) {
        const member = cellAt(row, col);
        member.setAttribute('aria-selected', 'true');
        selected.push(member);
      }
    }
    if (focus) cell.focus();
  }

  function remember(revert) {
    changed = true;
    undo.push(revert);
    if (undo.length > 30) undo.shift();
  }

  function apply(changes, appended = []) {
    for (const {cell, after} of changes) write(cell, after);
    if (!changes.length && !appended.length) return;
    remember(() => {
      for (const {cell, before, after} of changes) {
        if (body.contains(cell) && value(cell) === after) write(cell, before);
      }
      for (const row of appended) {
        if (dataCells(row).every(cell => !value(cell))) row.remove();
      }
      renumber();
    });
  }

  function edit(cell, initial = value(cell)) {
    if (editing) return;
    editing = {cell, original: value(cell)};
    editor.value = initial;
    editor.setAttribute('aria-label', labels[cell.cellIndex - 1]);
    write(cell, initial);
    cell.append(editor);
    editor.focus();
    editor.setSelectionRange(initial.length, initial.length);
  }

  function finishEdit(save = true) {
    if (!editing) return;
    const {cell, original} = editing;
    const after = editor.value;
    editing = null;
    editor.remove();
    write(cell, original);
    if (save && after !== original) apply([{cell, before: original, after}]);
  }

  function replaceRows(rows = [], focus = false) {
    finishEdit();
    const fragment = document.createDocumentFragment();
    if (rows.length) {
      for (const row of rows) appendRow([row.source_text, row.context, row.translated_text], fragment);
    } else appendRow(undefined, fragment);
    body.replaceChildren(fragment);
    undo.length = 0;
    renumber();
    select(cellAt(0, 0), false, focus);
  }

  // Excel quotes fields containing embedded tabs, newlines, or quotes.
  function parseClipboard(text) {
    if (!/[\t\r\n]/.test(text)) return [[text]];
    const rows = [];
    let row = [], field = '', quoted = false, endedWithNewline = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      endedWithNewline = false;
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
        else if (ch === '"') quoted = false;
        else field += ch;
      } else if (ch === '"' && field === '') quoted = true;
      else if (ch === '\t') { row.push(field); field = ''; }
      else if (ch === '\r' || ch === '\n') {
        if (ch === '\r' && text[i + 1] === '\n') i++;
        row.push(field);
        rows.push(row);
        row = [];
        field = '';
        endedWithNewline = true;
      } else field += ch;
    }
    if (!endedWithNewline) rows.push([...row, field]);
    return rows;
  }

  function paste(event) {
    const cell = event.target.closest('.manual-cell');
    if (!cell || event.target === editor || !event.clipboardData) return;
    const text = event.clipboardData.getData('text/plain');
    if (!text && !event.clipboardData.types.includes('text/plain')) return;
    event.preventDefault();
    const values = parseClipboard(text);
    const start = cell.parentNode.sectionRowIndex;
    const col = cell.cellIndex - 1;
    if (start + values.length > maxRows) {
      showError('手动输入最多支持 10000 行。');
      return;
    }
    const appended = [];
    const fragment = document.createDocumentFragment();
    for (let i = body.rows.length; i < start + values.length; i++) appended.push(appendRow(undefined, fragment));
    body.append(fragment);
    if (appended.length) renumber(body.rows.length - appended.length);
    const changes = [];
    let width = 1;
    for (const [offset, cells] of values.entries()) {
      width = Math.max(width, cells.length);
      for (let i = 0; i < Math.min(cells.length, labels.length - col); i++) {
        const target = cellAt(start + offset, col + i);
        const before = value(target);
        const after = normalize(cells[i]);
        if (before !== after) changes.push({cell: target, before, after});
      }
    }
    apply(changes, appended);
    // Keep keyboard focus on the paste origin and highlight the pasted block.
    anchor = cellAt(start + values.length - 1, Math.min(labels.length - 1, col + width - 1));
    select(cell, true);
    if (col + width > labels.length) showNotice('超出表格范围的列未粘贴。');
  }

  const tsvCell = text => /[\t\r\n"]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  function toTsv(rows) {
    return rows.map(row => row.map(cell => tsvCell(value(cell))).join('\t')).join('\r\n');
  }

  editor.addEventListener('input', () => write(editing.cell, editor.value));
  editor.addEventListener('blur', () => finishEdit());
  body.addEventListener('focusin', event => {
    if (event.target.matches('.manual-cell') && event.target !== active) select(event.target, false, false);
  });
  body.addEventListener('paste', paste);
  body.addEventListener('copy', event => {
    if (event.target === editor || !event.target.closest('.manual-cell') || !event.clipboardData) return;
    const rows = [];
    for (const cell of selected) {
      if (rows.at(-1)?.[0].parentNode !== cell.parentNode) rows.push([]);
      rows.at(-1).push(cell);
    }
    event.clipboardData.setData('text/plain', toTsv(rows));
    event.preventDefault();
  });
  body.addEventListener('pointerdown', event => {
    dragging = false;
    const cell = event.target.closest('.manual-cell');
    if (!cell || event.target === editor || event.button !== 0) return;
    event.preventDefault();
    finishEdit();
    dragging = event.pointerType === 'mouse';
    select(cell, event.shiftKey);
  });
  body.addEventListener('pointerover', event => {
    const cell = event.target.closest('.manual-cell');
    if (dragging && event.buttons === 1 && cell && cell !== active) select(cell, true);
  });
  document.addEventListener('pointerup', () => { dragging = false; });
  body.addEventListener('dblclick', event => {
    const cell = event.target.closest('.manual-cell');
    if (cell && event.target !== editor) edit(cell);
  });
  body.addEventListener('click', event => {
    const remove = event.target.closest('.manual-remove');
    if (!remove) return;
    const row = remove.closest('tr');
    const index = row.sectionRowIndex;
    const next = row.nextElementSibling;
    row.remove();
    const placeholder = body.rows.length ? null : appendRow();
    renumber(index);
    select(cellAt(Math.min(index, body.rows.length - 1), 0));
    remember(() => {
      if (placeholder && dataCells(placeholder).every(cell => !value(cell))) placeholder.remove();
      body.insertBefore(row, body.contains(next) ? next : null);
      renumber(index);
    });
  });
  body.addEventListener('keydown', event => {
    const cell = event.target.closest('.manual-cell');
    if (!cell || event.isComposing || event.keyCode === 229) return;
    const {key, shiftKey, altKey, ctrlKey, metaKey} = event;
    const row = cell.parentNode.sectionRowIndex;
    const col = cell.cellIndex - 1;
    const tabIndex = row * labels.length + col + (shiftKey ? -1 : 1);
    const tabCell = tabIndex < 0 ? null : cellAt(Math.floor(tabIndex / labels.length), tabIndex % labels.length);
    if (event.target === editor) {
      if (key === 'Enter' && altKey) {
        event.preventDefault();
        editor.setRangeText('\n', editor.selectionStart, editor.selectionEnd, 'end');
        write(cell, editor.value);
      } else if (['Escape', 'Enter', 'Tab'].includes(key) && !(key === 'Enter' && shiftKey)) {
        finishEdit(key !== 'Escape');
        select(key === 'Enter' ? (cellAt(row + 1, col) || cell) : (key === 'Tab' ? tabCell || cell : cell));
        if (key !== 'Tab' || tabCell) event.preventDefault();
      }
      return;
    }
    if (ctrlKey || metaKey) {
      if (key.toLowerCase() === 'z' && !shiftKey) {
        event.preventDefault();
        undo.pop()?.();
        select(body.contains(cell) ? cell : cellAt(body.rows.length - 1, col));
      }
      return;
    }
    const offset = {ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1]}[key];
    if (offset) {
      const [down, across] = offset;
      const nextCol = col + across;
      if (nextCol >= 0 && nextCol < labels.length) select(cellAt(row + down, nextCol), shiftKey);
    } else if (key === 'Tab' && tabCell) select(tabCell);
    else if (key === 'Enter' || key === 'F2') edit(cell);
    else if (key === 'Delete' || key === 'Backspace') {
      apply(selected.filter(member => value(member)).map(member => ({cell: member, before: value(member), after: ''})));
    } else if (key.length === 1 && !altKey) edit(cell, key);
    else return;
    event.preventDefault();
  });

  replaceRows();
  return {
    addRow() {
      if (body.rows.length >= maxRows) return showError('手动输入最多支持 10000 行。');
      const row = appendRow();
      renumber(row.sectionRowIndex);
      changed = true;
      select(row.cells[1]);
    },
    clear() {
      replaceRows([], true);
      changed = true;
    },
    replaceRows,
    get changed() { return changed; },
    hasContent: () => Array.from(body.rows).some(row => dataCells(row).some(cell => value(cell))),
    hasTranslations: () => Array.from(body.rows).some(row => value(row.cells[3])),
    tsv: translationsOnly => toTsv(Array.from(body.rows, row => translationsOnly ? [row.cells[3]] : dataCells(row))),
    snapshot() {
      finishEdit();
      return Array.from(body.rows, row => ({
        id: row.dataset.rowId, source_text: value(row.cells[1]), context: value(row.cells[2]), translated_text: value(row.cells[3])
      }));
    },
    applyResults(snapshot, results) {
      const byId = new Map(Array.from(body.rows, row => [row.dataset.rowId, row]));
      let skipped = 0;
      for (const [index, result] of results.entries()) {
        const original = snapshot[index];
        const row = original && byId.get(original.id);
        if (!row || editing?.cell === row.cells[3] || value(row.cells[1]) !== original.source_text ||
            value(row.cells[2]) !== original.context || value(row.cells[3]) !== original.translated_text) {
          skipped++;
        } else write(row.cells[3], result.translated_text);
      }
      return skipped;
    }
  };
};
