const caption = document.getElementById('caption');
const settingsButton = document.getElementById('settingsButton');

let socket = null;
let reconnectTimer = null;
let pingTimer = null;
let pendingText = '';
let sentenceQueue = [];
let sentenceTimer = null;
let settings = {
  connection: {
    wsUrl: 'ws://127.0.0.1:8765/ws',
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
  const parts = text.match(/[^.!?]+[.!?]+\s*/g);
  return parts ? parts.map(s => s.trim()).filter(Boolean) : [text.trim()];
}

function playNextSentence() {
  if (sentenceQueue.length === 0) return;
  const sentence = sentenceQueue.shift();
  setCaption(sentence);
  speakSentence(sentence);
}

function speakSentence(text) {
  if (!settings.voice.enabled || !text) {
    const wordCount = text.split(' ').length;
    sentenceTimer = setTimeout(playNextSentence, wordCount * 300 + 500);
    return;
  }

  const voices = 'speechSynthesis' in window ? window.speechSynthesis.getVoices() : [];
  const selectedVoice = voices.find(v => v.voiceURI === settings.voice.voiceURI);

  if (!selectedVoice) {
    window.torcsOverlay?.speak(text, settings.voice);
    const wordCount = text.split(' ').length;
    const ms = (wordCount / (settings.voice.rate * 2.5)) * 1000 + 600;
    sentenceTimer = setTimeout(playNextSentence, ms);
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
      setCaption('Generating captions...');
      break;
    case 'token':
      if (typeof message.text === 'string') {
        pendingText += message.text;
      }
      break;
    case 'ai_done': {
      const finalText = typeof message.content === 'string' && message.content.trim()
        ? message.content.trim()
        : pendingText.trim();
      const newSentences = splitSentences(finalText || 'Waiting for commentary...');
      const wasEmpty = sentenceQueue.length === 0;
      sentenceQueue.push(...newSentences);
      if (wasEmpty) playNextSentence();
      break;
    }
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

loadSettings().finally(connect);
