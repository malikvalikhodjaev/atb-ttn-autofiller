'use strict';

const fields = ['id', 'number', 'date', 'tin', 'comment'];
const labels = {id: 'ID СФ', number: 'Номер СФ', date: 'Дата СФ', tin: 'ИНН клиента', comment: 'Комментарий'};
const statusNames = {ready: 'Список проверен', invalid: 'Нужно исправить', duplicate: 'Повтор', conflict: 'Противоречие в списке'};
const state = {rows: [], source: null, selected: new Set(), filter: 'all', page: 0, dirty: false, busy: false, file: null};
const pageSize = 50;
const el = id => document.getElementById(id);

function showMessage(text, kind = '') {
  el('message').textContent = text;
  el('message').className = kind;
  el('message').hidden = !text;
}

function displaySettings(settings) {
  state.settings = settings;
  el('didox-url').value = settings.didox_url;
  el('smartup-url').value = settings.smartup_url;
  el('company-tin').value = settings.company_tin;
  el('loading-address').value = settings.loading_address;
  el('open-didox').href = settings.didox_url;
  el('open-smartup').hidden = !settings.smartup_url;
  if (settings.smartup_url) el('open-smartup').href = settings.smartup_url;
  else el('open-smartup').removeAttribute('href');
  if (typeof displayApiSettings === 'function') displayApiSettings(settings);
}

async function loadSettings() {
  try {
    const response = await fetch('/api/settings');
    if (!response.ok) await apiError(response);
    displaySettings((await response.json()).settings);
  } catch (error) {
    el('settings-message').textContent = error.message === 'Failed to fetch' ? 'Запустите «start.cmd», чтобы сохранить настройки.' : error.message;
    el('settings-message').classList.add('error');
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const controls = [...el('settings-form').querySelectorAll('input,button')];
  controls.forEach(control => {control.disabled = true;});
  try {
    const settings = {...(state.settings || {}), schema_version: 2, didox_url: el('didox-url').value, smartup_url: el('smartup-url').value,
      company_tin: el('company-tin').value, loading_address: el('loading-address').value,
      ...(typeof readApiSettings === 'function' ? readApiSettings() : {})};
    const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({settings})});
    if (!response.ok) await apiError(response);
    displaySettings((await response.json()).settings);
    el('settings-message').classList.remove('error');
    el('settings-message').textContent = 'Настройки сохранены. Выполните вход и проверку доступов; после изменения справочников подготовьте СФ заново.';
    if (typeof refreshConnectionStatus === 'function') await refreshConnectionStatus();
  } catch (error) {
    el('settings-message').textContent = error.message;
    el('settings-message').classList.add('error');
  } finally {controls.forEach(control => {control.disabled = false;});}
}

function setBusy(busy) {
  state.busy = busy;
  el('choose-file').disabled = busy;
  el('file-input').disabled = busy;
  el('load-sheet').disabled = busy;
  const readyIds = new Set(state.rows.filter(row => row.status === 'ready').map(row => row.record_id));
  document.querySelectorAll('#invoice-rows input').forEach(input => {
    if (input.type !== 'checkbox') input.disabled = busy;
    else input.disabled = busy || state.dirty || !readyIds.has(input.dataset.recordId);
  });
  updateActions();
}

function updateActions() {
  const loaded = state.source !== null;
  el('new-list').disabled = state.busy || !loaded;
  el('validate').disabled = state.busy || !loaded || state.rows.length === 0;
  el('export-report').disabled = state.busy || state.dirty || state.rows.length === 0;
  el('export-list').disabled = state.busy || state.dirty || state.selected.size === 0;
  el('select-all').disabled = state.busy || state.dirty || !state.rows.some(row => row.status === 'ready');
  el('selection-label').textContent = `Выбрано: ${state.selected.size}${state.dirty ? ' · требуется проверка изменений' : ''}`;
  const ready = state.rows.filter(row => row.status === 'ready');
  el('select-all').checked = ready.length > 0 && ready.every(row => state.selected.has(row.record_id));
  el('select-all').indeterminate = state.selected.size > 0 && !el('select-all').checked;
  if (typeof updateIntegrationActions === 'function') updateIntegrationActions();
}

function dirty() {
  if (!state.dirty) {
    state.dirty = true;
    document.querySelectorAll('#invoice-rows input[type=checkbox]').forEach(input => {input.disabled = true;});
  }
  showMessage('Есть изменения. Нажмите «Проверить изменения» перед сохранением списка или отчёта.', 'warning');
  updateActions();
}

function filteredRows() {
  return state.rows.filter(row => state.filter === 'all' ||
    (state.filter === 'attention' ? ['invalid', 'conflict'].includes(row.status) : row.status === state.filter));
}

function render() {
  const counts = {total: state.rows.length, ready: 0, attention: 0, duplicate: 0};
  state.rows.forEach(row => {
    if (row.status === 'ready') counts.ready += 1;
    else if (row.status === 'duplicate') counts.duplicate += 1;
    else counts.attention += 1;
  });
  el('count-total').textContent = counts.total;
  el('count-ready').textContent = counts.ready;
  el('count-attention').textContent = counts.attention;
  el('count-attention').classList.toggle('attention', counts.attention > 0);
  el('count-duplicates').textContent = counts.duplicate;
  el('source-label').textContent = state.source ? (state.source.origin === 'didox' ? state.source.description : `Файл: «${state.source.file_name}». Лист: «${state.source.sheet}».`) : 'Список ещё не загружен.';
  const visible = filteredRows();
  state.page = Math.max(0, Math.min(state.page, Math.ceil(visible.length / pageSize) - 1));
  const start = state.page * pageSize;
  const body = el('invoice-rows');
  body.replaceChildren();
  visible.slice(start, start + pageSize).forEach(row => {
    const tr = document.createElement('tr');
    const checkCell = document.createElement('td');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.dataset.recordId = row.record_id;
    checkbox.checked = state.selected.has(row.record_id);
    checkbox.disabled = state.dirty || state.busy || row.status !== 'ready';
    checkbox.setAttribute('aria-label', `Выбрать СФ из строки ${row.source_row}`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(row.record_id);
      else state.selected.delete(row.record_id);
      updateActions();
    });
    checkCell.append(checkbox);
    tr.append(checkCell);
    const number = document.createElement('td');
    number.className = 'source-row';
    number.textContent = row.source_row;
    tr.append(number);
    fields.forEach(field => {
      const td = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.disabled = state.busy;
      input.value = row.values[field];
      input.maxLength = 4000;
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.setAttribute('aria-label', `«${labels[field]}», строка ${row.source_row}`);
      if (field === 'id') input.classList.add('id-field');
      if (field === 'comment') input.classList.add('comment-field');
      if (row.field_errors[field]) {
        input.classList.add('invalid-field');
        input.title = row.field_errors[field];
        input.setAttribute('aria-invalid', 'true');
      }
      input.addEventListener('input', () => {
        row.values[field] = input.value;
        delete row.source_issues[field];
        dirty();
      });
      td.append(input);
      tr.append(td);
    });
    const statusCell = document.createElement('td');
    statusCell.className = 'status-cell';
    const status = document.createElement('span');
    status.className = `status-label ${row.status}`;
    status.textContent = statusNames[row.status];
    statusCell.append(status);
    if (row.errors.length) {
      const list = document.createElement('ul');
      list.className = 'row-errors';
      row.errors.forEach(message => {const item = document.createElement('li'); item.textContent = message; list.append(item);});
      statusCell.append(list);
    }
    tr.append(statusCell);
    body.append(tr);
  });
  el('empty-state').hidden = state.rows.length > 0;
  el('table-wrapper').hidden = state.rows.length === 0;
  el('pagination').hidden = state.rows.length === 0;
  el('page-label').textContent = visible.length ? `Строки ${start + 1}–${Math.min(start + pageSize, visible.length)} из ${visible.length}` : 'Нет строк с выбранным результатом проверки.';
  el('previous-page').disabled = state.page === 0;
  el('next-page').disabled = start + pageSize >= visible.length;
  updateActions();
}

async function apiError(response) {
  let data;
  try {data = await response.json();} catch {data = {error: 'Не удалось получить ответ обработчика.'};}
  const error = new Error(data.error || 'Не удалось обработать запрос.');
  error.sheets = data.sheets || [];
  throw error;
}

async function importFile(file, sheet = '') {
  if (!file) return;
  if (!/\.(xlsx|csv)$/i.test(file.name)) {
    showMessage('Выберите XLSX или CSV. Старый XLS сохраните в формате XLSX.', 'error');
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    showMessage('Максимальный размер файла — 15 МБ.', 'error');
    return;
  }
  state.file = file;
  setBusy(true);
  showMessage('Читаю файл и проверяю строки…');
  el('sheet-picker').hidden = true;
  try {
    const params = new URLSearchParams({filename: file.name});
    if (sheet) params.set('sheet', sheet);
    const response = await fetch(`/api/import?${params}`, {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file});
    if (!response.ok) await apiError(response);
    const data = await response.json();
    state.rows = data.rows;
    state.source = data.source;
    if (typeof resetInvoiceSearch === 'function') resetInvoiceSearch();
    state.dirty = false;
    state.page = 0;
    state.selected = new Set(data.rows.filter(row => row.status === 'ready').map(row => row.record_id));
    if (data.rows.length) showMessage(`Список загружен. СФ: ${data.summary.total}. Проверено: ${data.summary.ready}. Загрузка списка не создаёт ТТН.`);
    else showMessage('Файл прочитан, но заполненных строк СФ нет. Внесите свой список в шаблон и загрузите файл снова.', 'warning');
    render();
  } catch (error) {
    if (error.sheets?.length) {
      el('sheet-select').replaceChildren();
      error.sheets.forEach(name => {const option = document.createElement('option'); option.value = name; option.textContent = name; el('sheet-select').append(option);});
      el('sheet-picker').hidden = false;
    }
    showMessage(error.message === 'Failed to fetch' ? 'Обработчик недоступен. Запустите «start.cmd» и повторите загрузку.' : error.message, 'error');
  } finally {setBusy(false);}
}

async function revalidate() {
  setBusy(true);
  showMessage('Проверяю изменения…');
  try {
    const oldStatus = new Map(state.rows.map(row => [row.record_id, row.status]));
    const response = await fetch('/api/validate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rows: state.rows})});
    if (!response.ok) await apiError(response);
    const data = await response.json();
    state.rows = data.rows;
    state.selected = new Set(data.rows.filter(row => row.status === 'ready' && (state.selected.has(row.record_id) || oldStatus.get(row.record_id) !== 'ready')).map(row => row.record_id));
    state.dirty = false;
    showMessage(`Проверка завершена. Проверено: ${data.summary.ready}. На исправление: ${data.summary.invalid + data.summary.conflict}. Повторы: ${data.summary.duplicate}.`);
    render();
  } catch (error) {showMessage(error.message, 'error');}
  finally {setBusy(false); render();}
}

async function download(kind) {
  setBusy(true);
  try {
    const response = await fetch('/api/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind, rows: state.rows, selected_ids: [...state.selected]})});
    if (!response.ok) await apiError(response);
    const objectUrl = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = kind === 'report' ? 'sf_validation_report.csv' : 'sf_selected_list.xlsx';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    showMessage(kind === 'report' ? 'Отчёт подготовлен к сохранению. В нём указано состояние каждой строки.' : 'Выбранные СФ подготовлены к сохранению отдельным списком.');
  } catch (error) {showMessage(error.message, 'error');}
  finally {setBusy(false);}
}

el('settings-form').addEventListener('submit', saveSettings);
el('choose-file').addEventListener('click', () => el('file-input').click());
el('file-input').addEventListener('change', event => importFile(event.target.files[0]));
el('validate').addEventListener('click', revalidate);
el('export-report').addEventListener('click', () => download('report'));
el('export-list').addEventListener('click', () => download('list'));
el('load-sheet').addEventListener('click', () => importFile(state.file, el('sheet-select').value));
el('select-all').addEventListener('change', event => {
  state.selected = event.target.checked ? new Set(state.rows.filter(row => row.status === 'ready').map(row => row.record_id)) : new Set();
  render();
});
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.filter;
  state.page = 0;
  document.querySelectorAll('[data-filter]').forEach(item => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  render();
}));
el('previous-page').addEventListener('click', () => {state.page -= 1; render();});
el('next-page').addEventListener('click', () => {state.page += 1; render();});
el('new-list').addEventListener('click', () => {
  state.rows = []; state.source = null; state.selected.clear(); state.dirty = false; state.file = null;
  el('file-input').value = ''; el('sheet-picker').hidden = true; showMessage(''); render();
  if (typeof resetInvoiceSearch === 'function') resetInvoiceSearch();
});
['dragover', 'dragenter'].forEach(name => el('drop-zone').addEventListener(name, event => {
  event.preventDefault(); if (!state.busy) el('drop-zone').classList.add('dragging');
}));
['dragleave', 'drop'].forEach(name => el('drop-zone').addEventListener(name, event => {
  event.preventDefault(); el('drop-zone').classList.remove('dragging');
  if (name === 'drop' && !state.busy) {
    if (event.dataTransfer.files.length !== 1) showMessage('Перетащите один файл со списком СФ.', 'error');
    else importFile(event.dataTransfer.files[0]);
  }
}));
render();
loadSettings();
