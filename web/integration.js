'use strict';

const integrationState = {busy: false, connected: false, didox: false, smartup: false, demo: false, jobs: [], selected: new Set(),
  catalogs: {regions: [], districts: [], warehouses: []}, certificates: [], key: null,
  buffers: new Map(), expanded: new Set(), driverRows: [], driverOrg: '', search: null};
const jobNames = {ready: 'Готов к созданию', needs_review: 'Нужно заполнить поля', creating: 'Создание: результат проверить',
  uncertain: 'Результат создания неизвестен', draft: 'Черновик в Didox', signing: 'Подписание: результат проверить',
  sign_uncertain: 'Результат подписи неизвестен', signed: 'Подписан и отправлен', remote_document: 'Документ найден в Didox'};

async function integrationApi(path, payload) {
  const response = await fetch(`/api/integration/${path}`, payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  if (!response.ok) await apiError(response);
  return response.json();
}

function updateIntegrationActions() {
  const busy = integrationState.busy || state.busy;
  const count = state.rows.filter(row => state.selected.has(row.record_id) && row.status === 'ready').length;
  el('prepare-one').disabled = busy || state.dirty || !integrationState.connected || count !== 1;
  el('prepare-selected').disabled = busy || state.dirty || !integrationState.connected || count < 1 || count > 50;
  const chosen = integrationState.jobs.filter(job => integrationState.selected.has(job.job_id) && !integrationState.buffers.has(job.job_id));
  el('create-drafts').disabled = busy || !integrationState.connected || !el('reviewed').checked || !chosen.some(job => job.status === 'ready');
  el('sign-drafts').disabled = busy || integrationState.demo || !integrationState.didox || !el('reviewed').checked || !chosen.some(job => job.status === 'draft');
  ['connect-systems', 'disconnect-systems', 'load-keys', 'refresh-jobs'].forEach(id => {el(id).disabled = busy;});
  el('load-keys').disabled = busy || integrationState.demo;
  el('load-catalogs').disabled = busy || !integrationState.didox;
  el('search-invoices').disabled = busy || !integrationState.didox;
  el('more-invoices').disabled = busy || !integrationState.didox || !integrationState.search?.hasMore;
  el('get-drivers').disabled = busy || !integrationState.smartup;
  el('save-drivers').disabled = busy || !integrationState.driverRows.length;
  el('download-drivers').disabled = busy || !integrationState.driverRows.length;
}

async function integrationBusy(action) {
  if (integrationState.busy) return;
  integrationState.busy = true;
  setBusy(true);
  try {await action();}
  catch (error) {showMessage(error.message === 'Failed to fetch' ? 'Сервер недоступен. Запустите «start.cmd» и откройте страницу снова.' : error.message, 'error');}
  finally {integrationState.busy = false; setBusy(false); updateIntegrationActions();}
}

function asRecords(value) {return Array.isArray(value) ? value : Array.isArray(value?.data) ? value.data : [];}

function selectItems(select, items, selected, valueKey, labelKey, empty) {
  select.replaceChildren();
  const blank = document.createElement('option'); blank.value = ''; blank.textContent = empty; select.append(blank);
  items.forEach(item => {
    const option = document.createElement('option'); option.value = String(item[valueKey]); option.textContent = item[labelKey];
    option.selected = String(item[valueKey]) === String(selected); select.append(option);
  });
}

function displayApiSettings(settings) {
  el('search-company').textContent = `Исходящие СФ организации «${settings.company_tin || 'укажите ИНН в настройках'}». Получение списка ничего не создаёт; отметьте нужные строки после поиска.`;
  el('didox-api-url').value = settings.didox_api_url;
  el('smartup-api-url').value = settings.smartup_api_url;
  el('smartup-project').value = settings.smartup_project_code;
  el('smartup-filial').value = settings.smartup_filial_id;
  el('smartup-filial-code').value = settings.smartup_filial_code;
  el('reference-json').value = JSON.stringify({loading_point: settings.loading_point, drivers: settings.drivers,
    destinations: settings.destinations, distances: settings.distances}, null, 2);
  const point = settings.loading_point || {};
  let regions = integrationState.catalogs.regions;
  if (!regions.length && point.regionId) regions = [{regionId: point.regionId, name: point.regionName}];
  selectItems(el('loading-region'), regions, point.regionId, 'regionId', 'name', 'Выберите регион');
  renderLoadingDistricts(point);
}

function renderLoadingDistricts(point = {}) {
  let districts = integrationState.catalogs.districts.filter(item => String(item.regionId) === el('loading-region').value);
  if (!districts.length && point.districtCode) districts = [{districtCode: point.districtCode, name: point.districtName}];
  selectItems(el('loading-district'), districts, point.districtCode, 'districtCode', 'name', 'Выберите район');
}

function readApiSettings() {
  let references;
  try {references = JSON.parse(el('reference-json').value || '{}');}
  catch {throw new Error('Исправьте JSON справочников.');}
  if (!references || Array.isArray(references) || typeof references !== 'object') throw new Error('Справочники должны быть JSON-объектом.');
  const point = {...(references.loading_point || {}), address: el('loading-address').value};
  if (el('loading-region').value) {
    point.regionId = Number(el('loading-region').value);
    point.regionName = el('loading-region').selectedOptions[0].textContent;
  }
  if (el('loading-district').value) {
    point.districtCode = Number(el('loading-district').value);
    point.districtName = el('loading-district').selectedOptions[0].textContent;
  }
  return {...references, loading_point: point, didox_api_url: el('didox-api-url').value, smartup_api_url: el('smartup-api-url').value,
    smartup_project_code: el('smartup-project').value, smartup_filial_id: el('smartup-filial').value, smartup_filial_code: el('smartup-filial-code').value};
}

async function refreshConnectionStatus() {
  const response = await fetch('/api/status');
  if (!response.ok) await apiError(response);
  const status = await response.json();
  integrationState.connected = status.can_create_ttn;
  integrationState.didox = status.didox_connected;
  integrationState.smartup = status.smartup_connected;
  integrationState.demo = status.api_environment === 'loopback';
  el('didox-status').textContent = status.didox_connected ? (integrationState.demo ? 'локальный имитатор' : 'доступ проверен') : 'не подключён';
  el('smartup-status').textContent = status.smartup_connected ? (integrationState.demo ? 'локальный имитатор' : 'доступ проверен') : 'не подключён';
  if (integrationState.demo) el('connection-message').textContent = 'Демонстрационные API на этом компьютере: реальные документы и ЭЦП не используются.';
  updateIntegrationActions();
}

function resetInvoiceSearch() {integrationState.search = null; updateIntegrationActions();}

async function searchInvoices(event, more = false) {
  if (event) event.preventDefault();
  await integrationBusy(async () => {
    if (state.dirty) throw new Error('Проверьте и сохраните изменения текущего списка перед поиском.');
    const filters = more && integrationState.search ? {...integrationState.search.filters, page: integrationState.search.page + 1} : {
      date_from: el('invoice-from').value, date_to: el('invoice-to').value,
      partner: el('invoice-partner').value.trim(), status: el('invoice-status').value, page: 1};
    const company = state.settings?.company_tin || '';
    if (more && company !== integrationState.search.company) throw new Error('Организация изменилась. Выполните поиск заново.');
    el('search-message').textContent = 'Получаю список СФ…';
    const result = await integrationApi('search-invoices', {filters});
    const oldRows = more ? state.rows : [];
    const ids = new Set(oldRows.map(row => row.values.id));
    const added = result.rows.filter(row => !ids.has(row.values.id));
    added.forEach((row, index) => {row.source_row = oldRows.length + index + 2; row.record_id = `row-${row.source_row}`;});
    state.rows = [...oldRows, ...added];
    state.source = {origin: 'didox', description: `Didox · организация «${company}» · СФ от «${filters.date_from}» до «${filters.date_to}» · статус «${filters.status}»${filters.partner ? ` · покупатель «${filters.partner}»` : ''}`};
    if (!more) {
      state.selected.clear(); state.page = 0; state.filter = 'all';
      document.querySelectorAll('[data-filter]').forEach(button => {
        button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all'));
      });
    }
    state.dirty = false; state.file = null;
    integrationState.search = {filters, page: result.page, company, hasMore: result.has_more && !result.limit_reached};
    render();
    el('search-message').textContent = `В списке «${state.rows.length}» СФ. ${result.limit_reached ? 'Достигнут предел 2000 записей; сузьте период.' : result.has_more ? 'Есть следующая страница.' : 'Получены все страницы.'} Отметьте нужные строки для обработки или выгрузки Excel.`;
    showMessage('Список получен из Didox. Отметьте одну или несколько СФ; получение списка не создаёт ТТН.');
  });
}

const driverFields = {van_code: 'Код авто Smartup', driver_name: 'ФИО водителя', vehicle_number: 'Госномер', vehicle_model: 'Модель', driver_pinfl: 'ПИНФЛ'};

function renderDriverRows() {
  const parent = el('drivers-table'); parent.replaceChildren();
  if (!integrationState.driverRows.length) return;
  const table = document.createElement('table');
  const head = document.createElement('thead'); const titles = document.createElement('tr');
  Object.values(driverFields).forEach(label => {const th = document.createElement('th'); th.textContent = `«${label}»`; titles.append(th);});
  head.append(titles); table.append(head);
  const body = document.createElement('tbody');
  integrationState.driverRows.forEach((driver, index) => {
    const row = document.createElement('tr');
    Object.entries(driverFields).forEach(([key, label]) => {
      const cell = document.createElement('td'); const input = document.createElement('input'); input.type = 'text'; input.autocomplete = 'off';
      input.value = driver[key] || ''; input.maxLength = key === 'driver_pinfl' ? 14 : 300;
      input.readOnly = key === 'driver_name' || key === 'van_code';
      input.setAttribute('aria-label', `«${label}», водитель ${index + 1}`);
      if (key === 'driver_pinfl') input.inputMode = 'numeric';
      input.addEventListener('input', () => {driver[key] = input.value.trim();}); cell.append(input); row.append(cell);
    }); body.append(row);
  }); table.append(body); parent.append(table);
}

async function getDrivers() {
  await integrationBusy(async () => {
    el('drivers-message').textContent = 'Получаю архивные заказы Smartup…';
    const result = await integrationApi('driver-candidates', {date_from: el('drivers-from').value, date_to: el('drivers-to').value});
    integrationState.driverRows = result.drivers; integrationState.driverOrg = state.settings?.company_tin || '';
    renderDriverRows(); el('drivers-message').textContent = `Найдено водителей/машин: «${result.drivers.length}». ${result.note}`;
  });
}

async function saveDriverRows() {
  await integrationBusy(async () => {
    if (integrationState.driverOrg !== state.settings?.company_tin) throw new Error('Организация изменилась. Получите водителей заново.');
    const result = await integrationApi('save-drivers', {drivers: integrationState.driverRows});
    displaySettings(result.settings);
    el('drivers-message').textContent = `Сохранено записей: «${result.count}». Теперь подготовьте СФ заново, чтобы применить справочник.`;
  });
}

function downloadDriverRows() {
  const fields = Object.keys(driverFields);
  function cell(value) {const text = String(value || ''); return '"' + (/^[=+\-@]/.test(text.trimStart()) ? "'" : '') + text.replaceAll('"', '""') + '"';}
  const data = '\uFEFF' + [fields.join(';'), ...integrationState.driverRows.map(row => fields.map(key => cell(row[key])).join(';'))].join('\r\n') + '\r\n';
  const url = URL.createObjectURL(new Blob([data], {type: 'text/csv;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = 'smartup_drivers.csv'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function eimzoCall(request) {
  return new Promise((resolve, reject) => {
    let socket;
    try {socket = new WebSocket('wss://127.0.0.1:64443/service/cryptapi');}
    catch {reject(new Error('Не удалось открыть соединение с E-IMZO.')); return;}
    let finished = false;
    const timer = setTimeout(() => finish(null, 'E-IMZO не ответил за 60 секунд.'), 60000);
    function finish(result, message) {
      if (finished) return;
      finished = true; clearTimeout(timer); socket.close();
      if (message) reject(new Error(message)); else resolve(result);
    }
    socket.onopen = () => socket.send(JSON.stringify(request));
    socket.onerror = () => finish(null, 'Запустите E-IMZO и проверьте разрешение для локального сайта по официальной инструкции; TLS-проверку отключать не нужно.');
    socket.onclose = () => {if (!finished) finish(null, 'E-IMZO закрыл соединение.');};
    socket.onmessage = event => {
      try {
        const response = JSON.parse(event.data);
        if (!response.success) finish(null, response.reason || response.message || 'E-IMZO отказал в операции.');
        else finish(response);
      } catch {finish(null, 'E-IMZO вернул некорректный ответ.');}
    };
  });
}

async function loadEimzoKeys() {
  const errors = [];
  let certificates = [];
  try {certificates = (await eimzoCall({plugin: 'pfx', name: 'list_all_certificates'})).certificates || [];}
  catch (error) {errors.push(error.message);}
  integrationState.certificates = certificates;
  el('eimzo-key').replaceChildren();
  certificates.forEach((certificate, index) => {
    const option = document.createElement('option'); option.value = String(index);
    option.textContent = `${certificate.name}: ${certificate.alias}`; el('eimzo-key').append(option);
  });
  try {
    const response = await eimzoCall({plugin: 'ckc', name: 'list_ckc'});
    if (response.devices?.length) {const option = document.createElement('option'); option.value = 'usb'; option.textContent = 'USB-токен E-IMZO'; el('eimzo-key').append(option);}
  } catch (error) {errors.push(error.message);}
  if (!el('eimzo-key').options.length) throw new Error(errors[0] || 'Ключи E-IMZO не найдены.');
  integrationState.key = null;
  el('connection-message').textContent = 'Выберите ключ правильной организации; пароль ключа вводится в E-IMZO.';
}

async function eimzoSign(encoded) {
  const selection = el('eimzo-key').value;
  if (selection === '') throw new Error('Загрузите ключи и выберите ключ ЭЦП.');
  let keyId = 'ckc';
  if (selection !== 'usb') {
    if (!integrationState.key || integrationState.key.selection !== selection) {
      const certificate = integrationState.certificates[Number(selection)];
      if (!certificate) throw new Error('Выберите доступный ключ ЭЦП.');
      const loaded = await eimzoCall({plugin: 'pfx', name: 'load_key', arguments: [certificate.disk, certificate.path, certificate.name, certificate.alias]});
      if (!loaded.keyId) throw new Error('E-IMZO не вернул ID ключа.');
      integrationState.key = {selection, keyId: loaded.keyId};
    }
    keyId = integrationState.key.keyId;
  }
  return eimzoCall({plugin: 'pkcs7', name: 'create_pkcs7', arguments: [encoded, keyId, 'no']});
}

async function connectSystems(event) {
  event.preventDefault();
  await integrationBusy(async () => {
    el('connection-message').textContent = 'Проверяю доступы…';
    const method = el('didox-login-method').value;
    const credentials = {didox_partner_token: el('didox-partner').value, didox_login_tax_id: el('didox-login-tax').value,
      smartup_username: el('smartup-login').value, smartup_password: el('smartup-password').value};
    if (method === 'password') credentials.didox_password = el('didox-secret').value;
    if (method === 'token') credentials.didox_user_token = el('didox-secret').value;
    if (method === 'eimzo') {
      const taxId = credentials.didox_login_tax_id || el('company-tin').value;
      if (!/^(\d{9}|\d{14})$/.test(taxId)) throw new Error('Укажите ИНН/ПИНФЛ для входа.');
      const signature = await eimzoSign(btoa(taxId));
      credentials.didox_signature = (await integrationApi('auth-timestamp', {partner_token: credentials.didox_partner_token,
        pkcs7: signature.pkcs7_64, signature_hex: signature.signature_hex})).signature;
    }
    const result = await integrationApi('connect', {credentials});
    await refreshConnectionStatus();
    el('connection-message').textContent = Object.entries(result.errors).map(([system, error]) => `${system}: ${error}`).join(' ') ||
      'Доступы проверены. Подготовьте одну СФ; после проверки можно выбрать две–три или больше.';
    if (result.didox_connected && result.smartup_connected) ['didox-partner', 'didox-secret', 'smartup-password'].forEach(id => {el(id).value = '';});
  });
}

function appendResult(message) {
  const item = document.createElement('div'); item.textContent = message; el('prepare-results').append(item);
}

async function prepareInvoices(onlyOne = false) {
  await integrationBusy(async () => {
    if (integrationState.buffers.size) throw new Error('Сначала проверьте и сохраните открытые изменения JSON ТТН.');
    const rows = state.rows.filter(row => state.selected.has(row.record_id) && row.status === 'ready');
    if (onlyOne && rows.length !== 1) throw new Error('Для первой проверки отметьте одну СФ.');
    if (!rows.length || rows.length > 50 || state.dirty) throw new Error('Выберите от одной до 50 проверенных строк.');
    el('prepare-results').replaceChildren();
    el('reviewed').checked = false;
    for (let index = 0; index < rows.length; index += 5) {
      const group = rows.slice(index, index + 5);
      showMessage(`Подготовка строк ${index + 1}–${index + group.length} из ${rows.length}…`);
      try {
        const response = await integrationApi('prepare', {rows: group, selected_ids: group.map(row => row.record_id)});
        for (const result of response.results) {
          const row = group.find(item => item.record_id === result.record_id);
          if (result.job) {
            integrationState.selected.add(result.job.job_id);
            appendResult(`Строка «${row.source_row}»: ${jobNames[result.job.status] || result.job.status}.`);
          } else {
            appendResult(`Строка «${row.source_row}»: ${result.error}`);
            if (result.candidates?.length) renderCandidates(row, result.candidates);
          }
        }
      } catch (error) {appendResult(`Строки ${index + 1}–${index + group.length}: ${error.message}`);}
      await refreshJobs();
    }
    showMessage('Подготовка завершена. Проверьте каждую ТТН и заполните недостающие поля. Создание черновиков выполняется отдельной кнопкой.');
  });
}

function renderCandidates(row, candidates) {
  const wrapper = document.createElement('div');
  const select = document.createElement('select'); select.setAttribute('aria-label', `Заказ Smartup для строки ${row.source_row}`);
  candidates.forEach(candidate => {
    const option = document.createElement('option'); option.value = candidate.order_id;
    option.textContent = `${candidate.order_id}: ${candidate.number}, ${candidate.driver}, ${candidate.vehicle}`; select.append(option);
  });
  const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = 'Использовать этот заказ';
  button.addEventListener('click', async () => {
    await integrationBusy(async () => {
      const data = await integrationApi('prepare', {rows: [row], selected_ids: [row.record_id], order_id: select.value});
      if (data.results[0].job) {integrationState.selected.add(data.results[0].job.job_id); wrapper.remove(); await refreshJobs();}
      else throw new Error(data.results[0].error);
    });
  });
  wrapper.append(select, button); el('prepare-results').append(wrapper);
}

async function refreshJobs() {
  integrationState.jobs = (await integrationApi('jobs')).jobs;
  const ids = new Set(integrationState.jobs.map(job => job.job_id));
  integrationState.selected = new Set([...integrationState.selected].filter(id => ids.has(id)));
  for (const id of integrationState.buffers.keys()) {
    if (!integrationState.jobs.some(job => job.job_id === id && ['ready', 'needs_review'].includes(job.status))) integrationState.buffers.delete(id);
  }
  renderJobs(); updateIntegrationActions();
}

function renderJobs() {
  const parent = el('ttn-jobs'); parent.replaceChildren();
  if (!integrationState.jobs.length) {const note = document.createElement('p'); note.className = 'muted'; note.textContent = 'Пока нет подготовленных ТТН.'; parent.append(note); return;}
  integrationState.jobs.forEach(job => {
    const article = document.createElement('article'); article.className = 'ttn-job';
    const heading = document.createElement('div'); heading.className = 'ttn-job-header';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = integrationState.selected.has(job.job_id);
    checkbox.setAttribute('aria-label', `Выбрать ТТН по СФ ${job.number}`); checkbox.disabled = !['ready', 'draft'].includes(job.status) || integrationState.buffers.has(job.job_id);
    checkbox.addEventListener('change', () => {if (checkbox.checked) integrationState.selected.add(job.job_id); else integrationState.selected.delete(job.job_id); updateIntegrationActions();});
    const title = document.createElement('strong'); title.textContent = `СФ «${job.number}» от «${job.date}»`;
    const status = document.createElement('span'); status.textContent = jobNames[job.status] || job.status;
    heading.append(checkbox, title, status); article.append(heading);
    const sums = document.createElement('p'); sums.className = 'muted'; sums.textContent = `Заказ Smartup: «${job.order_id}» · с НДС: «${job.total_with_vat}» · груз без НДС: «${job.total_without_vat}»`; article.append(sums);
    if (job.error) {const error = document.createElement('p'); error.className = 'job-error'; error.textContent = job.error; article.append(error);}
    [['errors', ''], ['warnings', 'job-warning']].forEach(([name, className]) => {
      const list = document.createElement('ul'); list.className = className;
      job[name].forEach(message => {const item = document.createElement('li'); item.textContent = message; list.append(item);}); article.append(list);
    });
    const details = document.createElement('details');
    details.open = integrationState.expanded.has(job.job_id);
    details.addEventListener('toggle', () => {if (details.open) integrationState.expanded.add(job.job_id); else integrationState.expanded.delete(job.job_id);});
    const summary = document.createElement('summary'); summary.textContent = 'Посмотреть или исправить JSON ТТН'; details.append(summary);
    const editor = document.createElement('textarea'); editor.rows = 18; editor.spellcheck = false; editor.value = integrationState.buffers.get(job.job_id) ?? JSON.stringify(job.payload, null, 2);
    editor.setAttribute('aria-label', `JSON ТТН по СФ ${job.number}`); editor.disabled = !['ready', 'needs_review'].includes(job.status);
    editor.addEventListener('input', () => {
      integrationState.buffers.set(job.job_id, editor.value); el('reviewed').checked = false;
      integrationState.selected.delete(job.job_id); checkbox.checked = false; checkbox.disabled = true;
      const refsButton = article.querySelector('[data-save-references]'); if (refsButton) refsButton.disabled = true;
      updateIntegrationActions();
    });
    details.append(editor);
    if (['ready', 'needs_review'].includes(job.status)) {
      const save = document.createElement('button'); save.type = 'button'; save.className = 'secondary'; save.textContent = 'Проверить и сохранить JSON';
      save.addEventListener('click', () => integrationBusy(async () => {
        let payload; try {payload = JSON.parse(editor.value);} catch {throw new Error('Исправьте синтаксис JSON ТТН.');}
        const result = await integrationApi('edit', {job_id: job.job_id, payload});
        integrationState.buffers.delete(job.job_id);
        if (result.job.status === 'ready') integrationState.selected.add(job.job_id);
        el('reviewed').checked = false; await refreshJobs();
      }));
      details.append(save);
    }
    article.append(details);
    const actions = document.createElement('div'); actions.className = 'job-actions';
    if (['ready', 'draft'].includes(job.status)) {
      const saveRefs = document.createElement('button'); saveRefs.type = 'button'; saveRefs.className = 'secondary'; saveRefs.textContent = 'Сохранить адрес, водителя и км в справочники';
      saveRefs.dataset.saveReferences = ''; saveRefs.disabled = integrationState.buffers.has(job.job_id);
      saveRefs.addEventListener('click', () => integrationBusy(async () => {
        const result = await integrationApi('save-references', {job_id: job.job_id}); displaySettings(result.settings); await refreshJobs();
        showMessage('Справочники сохранены по проверенной ТТН. Подготовьте остальные СФ заново, чтобы применить эти значения.');
      }));
      actions.append(saveRefs);
    }
    if (job.waybill_id) {
      const identity = document.createElement('span'); identity.textContent = `ID ТТН: «${job.waybill_id}»`; actions.append(identity);
      const link = document.createElement('a'); link.href = `/api/integration/pdf?job_id=${encodeURIComponent(job.job_id)}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Открыть PDF из Didox'; actions.append(link);
    }
    if (['creating', 'uncertain', 'signing', 'sign_uncertain'].includes(job.status)) {
      const input = document.createElement('input'); input.type = 'text'; input.setAttribute('aria-label', `ID существующей ТТН для СФ ${job.number}`);
      const reconcile = document.createElement('button'); reconcile.type = 'button'; reconcile.className = 'secondary'; reconcile.textContent = 'Сверить найденную ТТН';
      reconcile.addEventListener('click', () => integrationBusy(async () => {await integrationApi('reconcile', {job_id: job.job_id, waybill_id: input.value.trim()}); await refreshJobs();}));
      actions.append(input, reconcile);
      if (!job.waybill_id && ['creating', 'uncertain'].includes(job.status)) {
        const check = document.createElement('input'); check.type = 'checkbox';
        const label = document.createElement('label'); label.append(check, document.createTextNode(' Проверил в Didox: ТТН не создана'));
        const absent = document.createElement('button'); absent.type = 'button'; absent.className = 'secondary'; absent.textContent = 'Разрешить повтор создания';
        absent.addEventListener('click', () => integrationBusy(async () => {await integrationApi('confirm-absent', {job_id: job.job_id, checked: check.checked}); await refreshJobs();}));
        actions.append(label, absent);
      }
    }
    article.append(actions); parent.append(article);
  });
}

async function createDrafts() {
  await integrationBusy(async () => {
    const jobs = integrationState.jobs.filter(job => integrationState.selected.has(job.job_id) && job.status === 'ready' && !integrationState.buffers.has(job.job_id));
    for (let index = 0; index < jobs.length; index++) {
      const job = jobs[index]; showMessage(`Создаю черновик ${index + 1} из ${jobs.length}: СФ «${job.number}»…`);
      try {
        const result = await integrationApi('create', {job_ids: [job.job_id], reviewed: el('reviewed').checked});
        const item = result.results[0]; appendResult(`СФ «${job.number}»: ${item.error || jobNames[item.status] || item.status}.`);
      } catch (error) {appendResult(`СФ «${job.number}»: ${error.message}`);}
      await refreshJobs();
    }
    showMessage('Создание завершено. Проверьте результат каждой СФ в журнале и PDF Didox.');
  });
}

async function signDrafts() {
  await integrationBusy(async () => {
    const jobs = integrationState.jobs.filter(job => integrationState.selected.has(job.job_id) && job.status === 'draft');
    for (let index = 0; index < jobs.length; index++) {
      const job = jobs[index]; showMessage(`Подписание ${index + 1} из ${jobs.length}: СФ «${job.number}»…`);
      try {
        const data = await integrationApi('sign-data', {job_id: job.job_id});
        const signature = await eimzoSign(data.data_base64);
        await integrationApi('sign', {job_id: job.job_id, challenge: data.challenge, pkcs7: signature.pkcs7_64, signature_hex: signature.signature_hex});
        appendResult(`СФ «${job.number}»: подписана и отправлена.`);
      } catch (error) {appendResult(`СФ «${job.number}»: ${error.message}`);}
      await refreshJobs();
    }
    showMessage('Операция подписи завершена. Результаты сохранены по каждой ТТН.');
  });
}

el('connection-form').addEventListener('submit', connectSystems);
el('didox-search-form').addEventListener('submit', event => searchInvoices(event));
el('more-invoices').addEventListener('click', () => searchInvoices(null, true));
el('get-drivers').addEventListener('click', getDrivers);
el('save-drivers').addEventListener('click', saveDriverRows);
el('download-drivers').addEventListener('click', downloadDriverRows);
el('disconnect-systems').addEventListener('click', () => integrationBusy(async () => {await integrationApi('disconnect', {}); await refreshConnectionStatus(); el('connection-message').textContent = 'Доступы удалены из памяти сервера.';}));
el('load-keys').addEventListener('click', () => integrationBusy(loadEimzoKeys));
el('prepare-one').addEventListener('click', () => prepareInvoices(true));
el('prepare-selected').addEventListener('click', () => prepareInvoices());
el('create-drafts').addEventListener('click', createDrafts);
el('sign-drafts').addEventListener('click', signDrafts);
el('reviewed').addEventListener('change', updateIntegrationActions);
el('refresh-jobs').addEventListener('click', () => integrationBusy(refreshJobs));
el('loading-region').addEventListener('change', () => renderLoadingDistricts());
el('warehouse-select').addEventListener('change', () => {
  const selected = integrationState.catalogs.warehouses.find(item => String(item.id) === el('warehouse-select').value);
  if (selected) el('loading-address').value = selected.warehouseAddress || '';
});
el('load-catalogs').addEventListener('click', () => integrationBusy(async () => {
  const response = await integrationApi('catalogs', {});
  integrationState.catalogs = {regions: asRecords(response.regions), districts: asRecords(response.districts), warehouses: asRecords(response.warehouses)};
  const current = {...(state.settings || {}), ...readApiSettings()}; displayApiSettings(current);
  selectItems(el('warehouse-select'), integrationState.catalogs.warehouses, '', 'id', 'warehouseName', 'Выберите склад');
  showMessage('Справочники загружены. Выберите регион, район, адрес и сохраните настройки.');
}));
el('drivers-file').addEventListener('change', event => integrationBusy(async () => {
  const file = event.target.files[0]; if (!file) return;
  const response = await fetch('/api/integration/drivers', {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file});
  if (!response.ok) await apiError(response);
  const result = await response.json(); displaySettings(result.settings); showMessage(`Справочник сохранён: водителей «${result.count}».`);
}));
if (state.settings) displayApiSettings(state.settings);
refreshConnectionStatus().catch(error => showMessage(error.message, 'error'));
refreshJobs().catch(error => showMessage(error.message, 'error'));
