// engineer-renderer.js — second overlay window for Feature 1 (AI Racing
// Engineer Chatbot). Clone of renderer.js, connecting to the same midware
// WebSocket, but only displaying messages tagged "source": "engineer" (see
// overlay_broadcast.py / docs/display-layer-contract.md). The generic
// "connected" lifecycle message has no source and is shown regardless, so
// this window still reports its own connection status.

const caption = document.getElementById('caption');
const settingsButton = document.getElementById('settingsButton');

let socket = null;
let reconnectTimer = null;
let pingTimer = null;
let pendingText = '';
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
  if ('speechSynthesis' in window) {
    window.speechSynthesis.cancel();
  }
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
  if (!message) {
    return;
  }

  // The connection-lifecycle message is shared by every feature and carries
  // no "source" field -- always show it so this window reports its own
  // connection status independently of the commentary window.
  if (message.type === 'connected') {
    setCaption('Waiting for engineer reply...');
    return;
  }

  // Everything else (ai_start/token/ai_done/error) must be explicitly
  // tagged for this feature, otherwise it belongs to another window
  // (e.g. race commentary) and should be ignored here.
  if (message.source !== 'engineer') {
    return;
  }

  switch (message.type) {
    case 'ai_start':
      pendingText = '';
      stopSpeech();
      setCaption('Generating engineer reply...');
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
      setCaption(finalText || 'Waiting for engineer reply...');
      speak(finalText);
      break;
    }
    case 'error': {
      const detail = conciseMessage(message.message);
      setCaption(detail ? `Engineer error: ${detail}` : 'Engineer error');
      break;
    }
    default:
      break;
  }
}

function connect() {
  setCaption('Connecting to engineer service...');
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
    setCaption('Waiting for engineer reply...');
    startPing();
  });

  nextSocket.addEventListener('message', (event) => {
    if (socket !== nextSocket) {
      return;
    }
    try {
      handleMessage(JSON.parse(event.data));
    } catch {
      setCaption('Engineer error');
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
