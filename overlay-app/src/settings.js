const ids = [
  'backendStatus', 'reloadBackend', 'wsUrl', 'reconnectDelayMs', 'pingIntervalMs',
  'saveOverlay', 'overlayNote', 'baseUrl', 'apiKey', 'model',
  'temperature', 'temperatureValue', 'stream', 'saveApi', 'apiNote',
  'voiceEnabled', 'voiceSelect',
  'voiceRate', 'voiceRateValue', 'voicePitch', 'voicePitchValue', 'voiceVolume',
  'voiceVolumeValue', 'testVoice', 'saveVoice', 'voiceNote',
  'ttsEnabled', 'ttsProvider', 'ttsUrl', 'ttsVoice', 'ttsSpeed', 'ttsSpeedValue',
  'ttsVolume', 'ttsVolumeValue', 'ttsCustomFields', 'ttsApiKey', 'ttsModel',
  'ttsResponseFormat', 'ttsResponseAudioField', 'ttsMime', 'ttsRequestTemplate',
  'saveTts', 'ttsNote'
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
  setRangeLabel(el.voiceRate, el.voiceRateValue, 1);
  setRangeLabel(el.voicePitch, el.voicePitchValue, 1);
  setRangeLabel(el.voiceVolume, el.voiceVolumeValue, 2);
  setRangeLabel(el.ttsSpeed, el.ttsSpeedValue, 1);
  setRangeLabel(el.ttsVolume, el.ttsVolumeValue, 2);
}

function updateTtsProviderUI() {
  el.ttsCustomFields.hidden = el.ttsProvider.value !== 'custom_http';
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
  const text = 'TORCS AI overlay voice test. The engineer overlay is ready.';
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

    el.baseUrl.value = config.api.base_url || '';
    el.apiKey.value = '';
    el.model.value = config.api.model || '';
    el.temperature.value = config.api.temperature ?? 0.8;
    el.stream.checked = Boolean(config.api.stream);

    const tts = config.tts || {};
    el.ttsEnabled.checked = Boolean(tts.enabled);
    el.ttsProvider.value = tts.provider || 'kokoro';
    el.ttsUrl.value = tts.url || '';
    el.ttsVoice.value = tts.voice || '';
    el.ttsSpeed.value = tts.speed ?? 1.2;
    el.ttsVolume.value = tts.volume ?? 1.0;
    el.ttsApiKey.value = '';
    el.ttsModel.value = tts.model || '';
    el.ttsResponseFormat.value = tts.response_format || 'audio_bytes';
    el.ttsResponseAudioField.value = tts.response_audio_field || '';
    el.ttsMime.value = tts.mime || '';
    el.ttsRequestTemplate.value = tts.request_template || '';
    updateTtsProviderUI();

    syncRangeLabels();
    note(el.apiNote, 'Backend configuration loaded.');
    note(el.ttsNote, 'TTS configuration loaded.');
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

async function saveTts() {
  try {
    const payload = {
      enabled: el.ttsEnabled.checked,
      provider: el.ttsProvider.value,
      url: el.ttsUrl.value.trim(),
      voice: el.ttsVoice.value.trim(),
      speed: Number(el.ttsSpeed.value),
      volume: Number(el.ttsVolume.value),
      model: el.ttsModel.value.trim(),
      response_format: el.ttsResponseFormat.value,
      response_audio_field: el.ttsResponseAudioField.value.trim(),
      mime: el.ttsMime.value.trim(),
      request_template: el.ttsRequestTemplate.value
    };

    if (el.ttsApiKey.value) {
      payload.api_key = el.ttsApiKey.value;
    }

    await request('/api/config/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    el.ttsApiKey.value = '';
    note(el.ttsNote, 'TTS settings saved.');
  } catch (error) {
    note(el.ttsNote, error.message, false);
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
  [el.voiceRate, () => setRangeLabel(el.voiceRate, el.voiceRateValue, 1)],
  [el.voicePitch, () => setRangeLabel(el.voicePitch, el.voicePitchValue, 1)],
  [el.voiceVolume, () => setRangeLabel(el.voiceVolume, el.voiceVolumeValue, 2)],
  [el.ttsSpeed, () => setRangeLabel(el.ttsSpeed, el.ttsSpeedValue, 1)],
  [el.ttsVolume, () => setRangeLabel(el.ttsVolume, el.ttsVolumeValue, 2)]
].forEach(([input, handler]) => input.addEventListener('input', handler));

el.reloadBackend.addEventListener('click', loadBackendConfig);
el.saveOverlay.addEventListener('click', saveOverlaySettings);
el.saveApi.addEventListener('click', saveApi);
el.saveTts.addEventListener('click', saveTts);
el.ttsProvider.addEventListener('change', updateTtsProviderUI);
el.saveVoice.addEventListener('click', saveOverlaySettings);
el.testVoice.addEventListener('click', speakTest);
el.wsUrl.addEventListener('change', loadBackendConfig);

if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

init();
