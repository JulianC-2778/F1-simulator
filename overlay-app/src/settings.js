const ids = [
  'backendStatus', 'reloadBackend', 'wsUrl', 'reconnectDelayMs', 'pingIntervalMs',
  'saveOverlay', 'overlayNote', 'baseUrl', 'apiKey', 'model',
  'temperature', 'temperatureValue', 'stream', 'saveApi', 'apiNote',
  'contextTokens', 'contextTokensValue', 'responseTokens', 'responseTokensValue',
  'persona', 'saveContext', 'contextNote', 'voiceEnabled', 'voiceSelect',
  'voiceRate', 'voiceRateValue', 'voicePitch', 'voicePitchValue', 'voiceVolume',
  'voiceVolumeValue', 'testVoice', 'saveVoice', 'voiceNote', 'autoMode',
  'baselineInterval', 'baselineIntervalValue', 'windowSeconds', 'windowSecondsValue',
  'eventCooldown', 'eventCooldownValue', 'dedupeSeconds', 'dedupeSecondsValue',
  'maxWords', 'maxWordsValue', 'saveAuto', 'autoNote', 'csvPath', 'rankingsPath',
  'loadCsv', 'injectDemo', 'manualCommentary', 'clearHistory', 'dataNote'
];

const el = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

let overlaySettings = null;
let voices = [];

function note(target, text, ok = true) {
  target.textContent = text;
  target.classList.toggle('ok', ok);
  target.classList.toggle('error', !ok);
}

function setRangeLabel(input, label, digits = 0) {
  const value = Number(input.value);
  label.textContent = digits > 0 ? value.toFixed(digits) : String(value);
}

function httpBaseFromWs(wsUrl) {
  try {
    const url = new URL(wsUrl);
    url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
    url.pathname = '';
    url.search = '';
    url.hash = '';
    return url.toString().replace(/\/$/, '');
  } catch {
    return 'http://127.0.0.1:8880';
  }
}

function backendBase() {
  return httpBaseFromWs(el.wsUrl.value.trim());
}

async function request(path, options = {}) {
  const response = await fetch(`${backendBase()}${path}`, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  }

  return data;
}

function collectOverlaySettings() {
  return {
    connection: {
      wsUrl: el.wsUrl.value.trim() || 'ws://127.0.0.1:8880/ws',
      reconnectDelayMs: Number(el.reconnectDelayMs.value) || 3000,
      pingIntervalMs: Number(el.pingIntervalMs.value) || 15000
    },
    voice: {
      enabled: el.voiceEnabled.checked,
      voiceURI: el.voiceSelect.value,
      rate: Number(el.voiceRate.value),
      pitch: Number(el.voicePitch.value),
      volume: Number(el.voiceVolume.value)
    }
  };
}

function populateOverlaySettings(settings) {
  overlaySettings = settings;
  el.wsUrl.value = settings.connection.wsUrl;
  el.reconnectDelayMs.value = settings.connection.reconnectDelayMs;
  el.pingIntervalMs.value = settings.connection.pingIntervalMs;
  el.voiceEnabled.checked = settings.voice.enabled;
  el.voiceRate.value = settings.voice.rate;
  el.voicePitch.value = settings.voice.pitch;
  el.voiceVolume.value = settings.voice.volume;
  syncRangeLabels();
  populateVoiceSelect(settings.voice.voiceURI);
}

function syncRangeLabels() {
  setRangeLabel(el.temperature, el.temperatureValue, 2);
  setRangeLabel(el.contextTokens, el.contextTokensValue);
  setRangeLabel(el.responseTokens, el.responseTokensValue);
  setRangeLabel(el.voiceRate, el.voiceRateValue, 1);
  setRangeLabel(el.voicePitch, el.voicePitchValue, 1);
  setRangeLabel(el.voiceVolume, el.voiceVolumeValue, 2);
  setRangeLabel(el.baselineInterval, el.baselineIntervalValue);
  setRangeLabel(el.windowSeconds, el.windowSecondsValue);
  setRangeLabel(el.eventCooldown, el.eventCooldownValue, 1);
  setRangeLabel(el.dedupeSeconds, el.dedupeSecondsValue);
  setRangeLabel(el.maxWords, el.maxWordsValue);
}

function populateVoiceSelect(selectedVoiceURI = '') {
  el.voiceSelect.textContent = '';

  const defaultOption = document.createElement('option');
  defaultOption.value = '';
  defaultOption.textContent = 'System default';
  el.voiceSelect.appendChild(defaultOption);

  voices.forEach((voice) => {
    const option = document.createElement('option');
    option.value = voice.voiceURI;
    option.textContent = `${voice.name} (${voice.lang})`;
    el.voiceSelect.appendChild(option);
  });

  el.voiceSelect.value = selectedVoiceURI;
}

function loadVoices() {
  if (!('speechSynthesis' in window)) {
    return;
  }

  voices = window.speechSynthesis.getVoices();
  populateVoiceSelect(overlaySettings ? overlaySettings.voice.voiceURI : '');
}

function speakTest() {
  const text = 'TORCS AI overlay voice test. The commentary system is ready.';
  const selectedVoice = voices.find((voice) => voice.voiceURI === el.voiceSelect.value);

  if (!selectedVoice || !('speechSynthesis' in window)) {
    window.torcsOverlay.speak(text, collectOverlaySettings().voice);
    note(el.voiceNote, 'Testing native system voice with speech-dispatcher.');
    return;
  }

  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = selectedVoice;
  utterance.lang = selectedVoice.lang;
  utterance.rate = Number(el.voiceRate.value);
  utterance.pitch = Number(el.voicePitch.value);
  utterance.volume = Number(el.voiceVolume.value);
  window.speechSynthesis.speak(utterance);
  note(el.voiceNote, 'Testing selected browser voice.');
}

async function loadBackendConfig() {
  try {
    el.backendStatus.textContent = `Connected to ${backendBase()}`;
    const config = await request('/api/config');
    const commentary = config.commentary || {};

    el.baseUrl.value = config.api.base_url || '';
    el.apiKey.value = '';
    el.model.value = config.api.model || '';
    el.temperature.value = config.api.temperature ?? 0.8;
    el.stream.checked = Boolean(config.api.stream);

    el.contextTokens.value = config.context.max_context_tokens || 4096;
    el.responseTokens.value = config.context.max_response_tokens || 512;
    el.persona.value = config.context.commentator_persona || '';

    el.autoMode.value = commentary.mode || (config.auto_interval > 0 ? 'interval' : 'off');
    el.baselineInterval.value = commentary.baseline_interval ?? config.auto_interval ?? 10;
    el.windowSeconds.value = commentary.window_seconds ?? 6;
    el.eventCooldown.value = commentary.event_cooldown ?? 1;
    el.dedupeSeconds.value = commentary.dedupe_seconds ?? 10;
    el.maxWords.value = commentary.max_words ?? 45;

    syncRangeLabels();
    note(el.apiNote, 'Backend configuration loaded.');
    note(el.contextNote, 'Context configuration loaded.');
    note(el.autoNote, 'Auto commentary configuration loaded.');
  } catch (error) {
    el.backendStatus.textContent = `Cannot reach ${backendBase()}`;
    note(el.apiNote, error.message, false);
  }
}

async function saveOverlaySettings() {
  try {
    overlaySettings = await window.torcsOverlay.saveSettings(collectOverlaySettings());
    populateOverlaySettings(overlaySettings);
    note(el.overlayNote, 'Overlay settings saved.');
    note(el.voiceNote, 'Voice settings saved.');
  } catch (error) {
    note(el.overlayNote, error.message, false);
  }
}

async function saveApi() {
  try {
    const payload = {
      base_url: el.baseUrl.value.trim(),
      model: el.model.value.trim(),
      temperature: Number(el.temperature.value),
      stream: el.stream.checked
    };

    if (el.apiKey.value) {
      payload.api_key = el.apiKey.value;
    }

    await request('/api/config/api', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    el.apiKey.value = '';
    note(el.apiNote, 'Model API saved.');
  } catch (error) {
    note(el.apiNote, error.message, false);
  }
}

async function saveContext() {
  try {
    const data = await request('/api/config/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        max_context_tokens: Number(el.contextTokens.value),
        max_response_tokens: Number(el.responseTokens.value),
        commentator_persona: el.persona.value
      })
    });
    note(el.contextNote, data.stats ? `Persona saved. Tokens: ${data.stats.total_tokens}` : 'Persona saved.');
  } catch (error) {
    note(el.contextNote, error.message, false);
  }
}

async function saveAuto() {
  try {
    await request('/api/commentary/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: el.autoMode.value,
        baseline_interval: Number(el.baselineInterval.value),
        window_seconds: Number(el.windowSeconds.value),
        event_cooldown: Number(el.eventCooldown.value),
        dedupe_seconds: Number(el.dedupeSeconds.value),
        max_words: Number(el.maxWords.value)
      })
    });
    note(el.autoNote, 'Auto commentary saved.');
  } catch (error) {
    note(el.autoNote, error.message, false);
  }
}

async function loadCsv() {
  try {
    const payload = { path: el.csvPath.value.trim() };
    if (el.rankingsPath.value.trim()) {
      payload.rankings_path = el.rankingsPath.value.trim();
    }
    const data = await request('/api/csv/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    note(el.dataNote, `Loaded ${data.rows_loaded} rows and queued commentary.`);
  } catch (error) {
    note(el.dataNote, error.message, false);
  }
}

async function injectDemo() {
  try {
    await request('/api/telemetry/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telemetry: {
          seq: 1,
          sim_time: 87.4,
          lap: 2,
          racePos: 2,
          speedX: 178.4,
          rpm: 6800,
          gear: 4,
          throttle: 0.92,
          brake: 0.0,
          steer: -0.15,
          damage: 0,
          fuel: 88.5,
          trackPos: 0.1,
          distFromStart: 1450
        },
        rankings: [
          { sim_time: 87.4, car_index: 0, car_name: 'scr_server 1', race_pos: 1, laps: 2, dist_from_start: 1501 },
          { sim_time: 87.4, car_index: 1, car_name: 'player 1', race_pos: 2, laps: 2, dist_from_start: 1450 },
          { sim_time: 87.4, car_index: 2, car_name: 'scr_server 2', race_pos: 3, laps: 2, dist_from_start: 1390 }
        ]
      })
    });
    note(el.dataNote, 'Demo telemetry injected.');
  } catch (error) {
    note(el.dataNote, error.message, false);
  }
}

async function triggerManualCommentary() {
  try {
    await request('/api/commentary/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: null })
    });
    note(el.dataNote, 'Commentary queued.');
  } catch (error) {
    note(el.dataNote, error.message, false);
  }
}

async function clearHistory() {
  try {
    await request('/api/commentary/clear', { method: 'POST' });
    note(el.dataNote, 'Commentary history cleared.');
  } catch (error) {
    note(el.dataNote, error.message, false);
  }
}

async function init() {
  overlaySettings = await window.torcsOverlay.getSettings();
  populateOverlaySettings(overlaySettings);
  loadVoices();
  await loadBackendConfig();
}

[
  [el.temperature, () => setRangeLabel(el.temperature, el.temperatureValue, 2)],
  [el.contextTokens, () => setRangeLabel(el.contextTokens, el.contextTokensValue)],
  [el.responseTokens, () => setRangeLabel(el.responseTokens, el.responseTokensValue)],
  [el.voiceRate, () => setRangeLabel(el.voiceRate, el.voiceRateValue, 1)],
  [el.voicePitch, () => setRangeLabel(el.voicePitch, el.voicePitchValue, 1)],
  [el.voiceVolume, () => setRangeLabel(el.voiceVolume, el.voiceVolumeValue, 2)],
  [el.baselineInterval, () => setRangeLabel(el.baselineInterval, el.baselineIntervalValue)],
  [el.windowSeconds, () => setRangeLabel(el.windowSeconds, el.windowSecondsValue)],
  [el.eventCooldown, () => setRangeLabel(el.eventCooldown, el.eventCooldownValue, 1)],
  [el.dedupeSeconds, () => setRangeLabel(el.dedupeSeconds, el.dedupeSecondsValue)],
  [el.maxWords, () => setRangeLabel(el.maxWords, el.maxWordsValue)]
].forEach(([input, handler]) => input.addEventListener('input', handler));

el.reloadBackend.addEventListener('click', loadBackendConfig);
el.saveOverlay.addEventListener('click', saveOverlaySettings);
el.saveApi.addEventListener('click', saveApi);
el.saveContext.addEventListener('click', saveContext);
el.saveAuto.addEventListener('click', saveAuto);
el.saveVoice.addEventListener('click', saveOverlaySettings);
el.testVoice.addEventListener('click', speakTest);
el.loadCsv.addEventListener('click', loadCsv);
el.injectDemo.addEventListener('click', injectDemo);
el.manualCommentary.addEventListener('click', triggerManualCommentary);
el.clearHistory.addEventListener('click', clearHistory);
el.wsUrl.addEventListener('change', loadBackendConfig);

if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

init();
