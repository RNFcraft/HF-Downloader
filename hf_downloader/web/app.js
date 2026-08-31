const $ = (id) => document.getElementById(id);
const state = { files: [], selected: new Set(), source: null, running: false, timer: null, initialized: false };
const ui = Object.fromEntries([
  'connection','source-url','repo-type','inspect','source-meta','file-summary','file-search','files',
  'select-all','clear-all','destination','browse','subfolder','workers','retries','transport-options','timeout',
  'token','exclude','selected-total','download','open-folder','sidebar-hint','progress-card','progress-title',
  'stop','progress-bar','progress-percent','progress-bytes','speed','average-speed','eta','files-done','active-files','toast',
  'attempt-value','active-transport','worker-state','heartbeat','current-file','event-log'
].map(id => [id, $(id)]));

const formatBytes = (value = 0) => {
  const units = ['Б','КБ','МБ','ГБ','ТБ']; let size = Math.max(0, Number(value) || 0); let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
  return unit ? `${size.toFixed(1)} ${units[unit]}` : `${Math.round(size)} Б`;
};
const formatDuration = (seconds) => {
  if (seconds == null || seconds < 0 || seconds > 31536000) return '—';
  const n = Math.floor(seconds), h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
  return h ? `${h} ч ${String(m).padStart(2,'0')} мин` : m ? `${m} мин ${String(s).padStart(2,'0')} с` : `${s} с`;
};
const toast = (message, error = false) => {
  ui.toast.textContent = message; ui.toast.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => ui.toast.className = 'toast', 4200);
};
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const appendLog = (message, kind = '') => {
  if (!message) return;
  const line = document.createElement('div');
  line.className = `log-line ${kind}`;
  line.innerHTML = `<time>${new Date().toLocaleTimeString('ru-RU')}</time>${escapeHtml(message)}`;
  ui['event-log'].appendChild(line); ui['event-log'].scrollTop = ui['event-log'].scrollHeight;
};

function fillSettings(data) {
  const s = data.settings;
  ui.destination.value = s.destination; ui['repo-type'].value = s.repo_type; ui.subfolder.checked = s.create_subfolder;
  ui.workers.value = s.workers; ui.retries.value = s.retries; ui.timeout.value = s.stall_timeout; ui.exclude.value = s.exclude;
  const selectedTransport = document.querySelector(`input[name="transport"][value="${s.transport}"]`);
  (selectedTransport || document.querySelector('input[name="transport"][value="auto"]')).checked = true;
}
function updateSelection() {
  const bytes = state.files.reduce((sum, f) => sum + (state.selected.has(f.path) ? f.size : 0), 0);
  ui['selected-total'].textContent = `${state.selected.size} файлов · ${formatBytes(bytes)}`;
  ui.download.disabled = !state.selected.size || state.running;
  ui['sidebar-hint'].textContent = state.selected.size ? 'Готово к загрузке.' : 'Отметьте хотя бы один файл.';
  ui['file-summary'].textContent = state.files.length ? `Выбрано ${state.selected.size} из ${state.files.length} · ${formatBytes(bytes)}` : 'Сначала укажите источник';
}
function renderFiles() {
  const query = ui['file-search'].value.trim().toLowerCase();
  const matches = state.files.filter(f => !query || f.path.toLowerCase().includes(query));
  if (!state.files.length) return;
  if (!matches.length) { ui.files.className = 'file-list empty-state'; ui.files.innerHTML = '<strong>Ничего не найдено</strong><span>Попробуйте изменить запрос.</span>'; return; }
  ui.files.className = 'file-list';
  ui.files.innerHTML = matches.slice(0, 5000).map(f => `<label class="file-row"><input type="checkbox" data-path="${escapeHtml(f.path)}" ${state.selected.has(f.path) ? 'checked' : ''}><span class="file-path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</span><span class="file-size">${formatBytes(f.size)}</span></label>`).join('');
  ui.files.querySelectorAll('input').forEach(box => box.addEventListener('change', () => { box.checked ? state.selected.add(box.dataset.path) : state.selected.delete(box.dataset.path); updateSelection(); }));
}
async function inspect() {
  const value = ui['source-url'].value.trim(); if (!value) return toast('Введите ссылку или ID репозитория.', true);
  ui.inspect.disabled = true; ui.inspect.innerHTML = '<i class="spinner"></i>'; ui['file-summary'].textContent = 'Получение дерева репозитория…';
  try {
    const result = await pywebview.api.inspect_source(value, ui['repo-type'].value, ui.token.value);
    if (!result.ok) throw new Error(result.error);
    state.source = result.source; state.files = result.files; state.selected = new Set(result.files.map(f => f.path));
    ui['source-meta'].classList.remove('hidden');
    ui['source-meta'].innerHTML = `<span>${escapeHtml(result.source.repo_type)}</span><span>${escapeHtml(result.source.repo_id)}</span><span>revision: ${escapeHtml(result.source.revision)}</span>${result.source.path ? `<span>${escapeHtml(result.source.path)}</span>` : ''}`;
    ui['file-search'].disabled = false; renderFiles(); updateSelection();
  } catch (error) { state.files = []; state.selected.clear(); updateSelection(); toast(error.message || String(error), true); ui['file-summary'].textContent = 'Не удалось получить список'; }
  finally { ui.inspect.disabled = false; ui.inspect.textContent = 'Получить файлы'; }
}
function options() { const transport = document.querySelector('input[name="transport"]:checked')?.value || 'auto'; return { url:ui['source-url'].value, repo_type:ui['repo-type'].value, destination:ui.destination.value, create_subfolder:ui.subfolder.checked, workers:ui.workers.value, retries:ui.retries.value, stall_timeout:ui.timeout.value, transport, token:ui.token.value, exclude:ui.exclude.value, selected_files:[...state.selected] }; }
async function startDownload() {
  ui.download.disabled = true;
  const result = await pywebview.api.start_download(options());
  if (!result.ok) { toast(result.error, true); updateSelection(); return; }
  state.running = true; ui['progress-card'].classList.remove('hidden'); ui['progress-title'].textContent = 'Подготовка плана…'; ui.stop.disabled = false;
  ui.inspect.disabled = true; ui['source-url'].disabled = true; ui['repo-type'].disabled = true; ui['sidebar-hint'].textContent = result.destination;
}
function handleEvent(event) {
  if (event.message) ui['progress-title'].textContent = event.message;
  if (event.message && ['status','plan','retry','fallback','complete','cancelled','error'].includes(event.kind)) appendLog(event.message, event.kind);
  if (event.total) { const pct = Math.min(100, event.downloaded / event.total * 100); ui['progress-bar'].style.width = `${pct}%`; ui['progress-percent'].textContent = `${pct.toFixed(1)}%`; ui['progress-bytes'].textContent = `${formatBytes(event.downloaded)} из ${formatBytes(event.total)}`; }
  if (event.attempt) ui['attempt-value'].textContent = event.attempt;
  if (event.transport) ui['active-transport'].textContent = event.transport;
  if (event.current_file) ui['current-file'].textContent = event.current_file;
  if (event.heartbeat_age != null) { ui.heartbeat.textContent = `${event.heartbeat_age.toFixed(1)} с`; ui['worker-state'].textContent = event.worker_alive ? 'Работает' : 'Нет ответа'; }
  if (event.kind === 'progress') { ui.speed.textContent = `${formatBytes(event.speed)}/с`; ui['average-speed'].textContent = `${formatBytes(event.average_speed)}/с`; ui.eta.textContent = formatDuration(event.eta); ui['files-done'].textContent = `${event.files_done} / ${event.files_total}`; const active = (event.active_files || []).filter(f => !f.total || f.downloaded < f.total); ui['active-files'].innerHTML = active.slice(0,6).map(f => `${escapeHtml(f.path)} — ${formatBytes(f.downloaded)} / ${formatBytes(f.total)}`).join('<br>'); }
  if (['complete','cancelled','error'].includes(event.kind)) { state.running = false; ui.stop.disabled = true; ui.inspect.disabled = false; ui['source-url'].disabled = false; ui['repo-type'].disabled = false; updateSelection(); toast(event.message || (event.kind === 'complete' ? 'Загрузка завершена' : 'Загрузка остановлена'), event.kind === 'error'); }
}
async function poll() { try { const events = await pywebview.api.poll_events(); events.forEach(handleEvent); } catch (_) {} finally { state.timer = setTimeout(poll, 250); } }

ui.inspect.addEventListener('click', inspect); ui['source-url'].addEventListener('keydown', e => { if (e.key === 'Enter') inspect(); });
ui['file-search'].addEventListener('input', renderFiles); ui['select-all'].addEventListener('click', () => { state.selected = new Set(state.files.map(f => f.path)); renderFiles(); updateSelection(); });
ui['clear-all'].addEventListener('click', () => { state.selected.clear(); renderFiles(); updateSelection(); });
ui.browse.addEventListener('click', async () => { const path = await pywebview.api.choose_destination(ui.destination.value); if (path) ui.destination.value = path; });
ui.download.addEventListener('click', startDownload); ui.stop.addEventListener('click', async () => { ui.stop.disabled = true; ui['progress-title'].textContent = 'Останавливаем worker…'; await pywebview.api.cancel_download(); });
ui['open-folder'].addEventListener('click', async () => { const result = await pywebview.api.open_destination(ui.destination.value); if (!result.ok) toast(result.error, true); });
async function initializeBridge() {
  if (state.initialized || !window.pywebview?.api) return;
  state.initialized = true;
  try {
    fillSettings(await pywebview.api.initial_state());
    ui.connection.classList.add('ready'); ui.connection.querySelector('span').textContent = 'Локальный режим';
    appendLog('Локальное Python-ядро готово. Сетевые запросы выполняются только к Hugging Face.', 'complete');
    poll();
  } catch (error) {
    state.initialized = false; ui.connection.querySelector('span').textContent = 'Ошибка локального ядра';
    toast(`Не удалось запустить backend: ${error}`, true);
  }
}
document.addEventListener('pywebviewready', initializeBridge);
if (window.pywebview?.api) initializeBridge();
const bridgeWatcher = setInterval(() => {
  if (window.pywebview?.api) {
    clearInterval(bridgeWatcher);
    initializeBridge();
  }
}, 100);
