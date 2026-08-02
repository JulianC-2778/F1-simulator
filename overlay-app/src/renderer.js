const caption = document.getElementById('caption');
const settingsButton = document.getElementById('settingsButton');

let socket = null;
let reconnectTimer = null;
let pingTimer = null;
let pendingText = '';
let sentenceQueue = [];
let sentenceTimer = null;
let serverTtsEnabled = false;
let currentTtsAudio = null;
let settings = {
  connection: {
    wsUrl: 'ws://127.0.0.1:8880/ws',
    reconnectDelayMs: 3000,
    pingIntervalMs: 15000
  },
  voice: {
    enabled: false,
    voiceURI: '',
    rate: 1.1,
    pitch: 1.0,
    volume: 1.0
  }
};

function setCaption(text) {
  caption.textContent = text;
  requestAnimationFrame(() => {
    window.torcsOverlay?.resizeWindow(document.body.scrollHeight);
  });
}

function splitSentences(text) {
  const parts = text.match(/[^.!?,，。！？]+[.!?,，。！？]+\s*/g);
  return parts ? parts.map(s => s.trim()).filter(Boolean) : [text.trim()];
}

// CJK characters carry roughly one "word" of speech duration each; Latin
// text is still counted by whitespace-separated words. Used only as a
// pacing estimate (silent captions) or as a safety-net timeout — the native
// speech path now advances on the real 'voice:speech-ended' event instead.
function estimateReadMs(text, rate = 1) {
  const cjkChars = (text.match(/[一-鿿]/g) || []).length;
  const latinWords = text.replace(/[一-鿿]/g, ' ').trim().split(/\s+/).filter(Boolean).length;
  return ((cjkChars + latinWords) * 300) / Math.max(rate, 0.1) + 500;
}

function playNextSentence() {
  if (sentenceQueue.length === 0) return;
  const sentence = sentenceQueue.shift();
  setCaption(sentence);
  speakSentence(sentence);
}

function speakSentence(text) {
  if (!settings.voice.enabled || !text) {
    sentenceTimer = setTimeout(playNextSentence, estimateReadMs(text));
    return;
  }

  const voices = 'speechSynthesis' in window ? window.speechSynthesis.getVoices() : [];
  const selectedVoice = voices.find(v => v.voiceURI === settings.voice.voiceURI);

  if (!selectedVoice) {
    window.torcsOverlay?.speak(text, settings.voice);
    // Safety net in case the native process never reports back.
    sentenceTimer = setTimeout(playNextSentence, estimateReadMs(text, settings.voice.rate) * 3);
    return;
  }

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = selectedVoice;
  utterance.lang = selectedVoice.lang;
  utterance.rate = settings.voice.rate;
  utterance.pitch = settings.voice.pitch;
  utterance.volume = settings.voice.volume;
  utterance.onend = () => playNextSentence();
  window.speechSynthesis.speak(utterance);
}

function clearTimers() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) {
    return;
  }

  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, settings.connection.reconnectDelayMs);
}

function startPing() {
  if (pingTimer) {
    clearInterval(pingTimer);
  }
  pingTimer = window.setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send('ping');
    }
  }, settings.connection.pingIntervalMs);
}

function stopSpeech() {
  sentenceQueue = [];
  if (sentenceTimer) { clearTimeout(sentenceTimer); sentenceTimer = null; }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  window.torcsOverlay?.stopSpeech();
}

function restBaseUrl() {
  return settings.connection.wsUrl.replace(/^ws/, 'http').replace(/\/ws\/?$/, '');
}

async function refreshTtsConfig() {
  try {
    const resp = await fetch(`${restBaseUrl()}/api/config`);
    const data = await resp.json();
    serverTtsEnabled = Boolean(data?.tts?.enabled);
  } catch {
    serverTtsEnabled = false;
  }
}

function playTtsAudio(base64, mime = 'audio/wav') {
  if (currentTtsAudio) { currentTtsAudio.pause(); currentTtsAudio = null; }
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  const blob = new Blob([bytes], { type: mime });
  const url = URL.createObjectURL(blob);
  currentTtsAudio = new Audio(url);
  currentTtsAudio.onended = () => { URL.revokeObjectURL(url); currentTtsAudio = null; };
  currentTtsAudio.play().catch(() => {});
}

function stopTtsAudio() {
  if (currentTtsAudio) { currentTtsAudio.pause(); currentTtsAudio = null; }
}

function speak(text) {
  if (!settings.voice.enabled || !text) {
    return;
  }

  stopSpeech();
  const voices = 'speechSynthesis' in window ? window.speechSynthesis.getVoices() : [];
  const selectedVoice = voices.find((voice) => voice.voiceURI === settings.voice.voiceURI);

  if (!selectedVoice) {
    window.torcsOverlay?.speak(text, settings.voice);
    return;
  }

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = selectedVoice;
  utterance.lang = selectedVoice.lang;

  utterance.rate = settings.voice.rate;
  utterance.pitch = settings.voice.pitch;
  utterance.volume = settings.voice.volume;
  window.speechSynthesis.speak(utterance);
}

function conciseMessage(message) {
  if (!message || typeof message !== 'string') {
    return '';
  }

  const trimmed = message.trim();
  if (!trimmed) {
    return '';
  }

  return trimmed.length > 80 ? `${trimmed.slice(0, 77)}...` : trimmed;
}

function handleMessage(message) {
  switch (message.type) {
    case 'connected':
      setCaption('Waiting for commentary...');
      break;
    case 'ai_start':
      pendingText = '';
      stopSpeech();
      stopTtsAudio();
      setCaption('Generating captions...');
      break;
    case 'token':
      if (typeof message.text === 'string') {
        pendingText += message.text;
      }
      break;
    case 'ai_done': {
      if (message.duplicate) {
        // Backend already decided this text was shown recently and
        // suppressed it -- discard whatever streamed into pendingText
        // instead of falling back to it below. Leave the existing caption
        // on screen rather than blanking it.
        pendingText = '';
        break;
      }
      const finalText = typeof message.content === 'string' && message.content.trim()
        ? message.content.trim()
        : pendingText.trim();
      const text = finalText || 'Waiting for commentary...';
      if (serverTtsEnabled) {
        // Kokoro TTS handles audio playback; it arrives separately via 'tts_audio'.
        setCaption(text);
      } else {
        const newSentences = splitSentences(text);
        const wasEmpty = sentenceQueue.length === 0;
        sentenceQueue.push(...newSentences);
        if (wasEmpty) playNextSentence();
      }
      break;
    }
    case 'tts_audio':
      stopSpeech();
      playTtsAudio(message.audio, message.mime);
      break;
    case 'config_updated':
      if (message.section === 'tts') refreshTtsConfig();
      break;
    case 'error': {
      const detail = conciseMessage(message.message);
      setCaption(detail ? `Commentary error: ${detail}` : 'Commentary error');
      break;
    }
    case 'telemetry_update':
    case 'event_detected':
      break;
    default:
      break;
  }
}

function connect() {
  setCaption('Connecting to commentary service...');
  clearTimers();

  if (socket) {
    const oldSocket = socket;
    socket = null;
    oldSocket.close();
  }

  let nextSocket;
  try {
    nextSocket = new WebSocket(settings.connection.wsUrl);
  } catch {
    setCaption('Connection lost');
    scheduleReconnect();
    return;
  }
  socket = nextSocket;

  nextSocket.addEventListener('open', () => {
    if (socket !== nextSocket) {
      return;
    }
    setCaption('Waiting for commentary...');
    startPing();
    refreshTtsConfig();
  });

  nextSocket.addEventListener('message', (event) => {
    if (socket !== nextSocket) {
      return;
    }
    try {
      handleMessage(JSON.parse(event.data));
    } catch {
      setCaption('Commentary error');
    }
  });

  nextSocket.addEventListener('error', () => {
    if (socket !== nextSocket) {
      return;
    }
    setCaption('Connection lost');
  });

  nextSocket.addEventListener('close', () => {
    if (socket !== nextSocket) {
      return;
    }
    clearTimers();
    setCaption('Connection lost');
    scheduleReconnect();
  });
}

async function loadSettings() {
  if (window.torcsOverlay) {
    settings = await window.torcsOverlay.getSettings();
  }
}

function applySettings(nextSettings) {
  const previousUrl = settings.connection.wsUrl;
  settings = nextSettings;

  if (settings.connection.wsUrl !== previousUrl) {
    connect();
  }
}

settingsButton.addEventListener('click', () => {
  window.torcsOverlay?.openSettings();
});

window.torcsOverlay?.onSettingsUpdated(applySettings);
window.torcsOverlay?.onSpeechEnded(() => {
  if (sentenceTimer) { clearTimeout(sentenceTimer); sentenceTimer = null; }
  playNextSentence();
});

loadSettings().finally(connect);
