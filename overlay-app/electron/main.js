const { app, BrowserWindow, Menu, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

let overlayWindow;
let engineerWindow;
let settingsWindow;
let speechProcess;

const defaultSettings = {
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

function settingsPath() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function mergeSettings(settings) {
  return {
    connection: {
      ...defaultSettings.connection,
      ...(settings && settings.connection ? settings.connection : {})
    },
    voice: {
      ...defaultSettings.voice,
      ...(settings && settings.voice ? settings.voice : {})
    }
  };
}

function readSettings() {
  try {
    const raw = fs.readFileSync(settingsPath(), 'utf8');
    return mergeSettings(JSON.parse(raw));
  } catch {
    return mergeSettings();
  }
}

function writeSettings(settings) {
  const merged = mergeSettings(settings);
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
  fs.writeFileSync(settingsPath(), `${JSON.stringify(merged, null, 2)}\n`);
  return merged;
}

function getWindowBounds() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { x, y, width, height } = primaryDisplay.workArea;
  const windowWidth = 900;
  const windowHeight = 160;

  return {
    width: windowWidth,
    height: windowHeight,
    x: Math.round(x + (width - windowWidth) / 2),
    y: Math.round(y + height - windowHeight - 56)
  };
}

// Feature 1 (AI Racing Engineer Chatbot) gets its own floating window so its
// replies never overwrite/compete with race commentary captions. Anchored
// near the top-center of the screen so the two windows never overlap.
function getEngineerWindowBounds() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { x, y, width } = primaryDisplay.workArea;
  const windowWidth = 900;
  const windowHeight = 160;

  return {
    width: windowWidth,
    height: windowHeight,
    x: Math.round(x + (width - windowWidth) / 2),
    y: Math.round(y + 56)
  };
}

function createOverlayWindow() {
  overlayWindow = new BrowserWindow({
    ...getWindowBounds(),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    fullscreenable: false,
    hasShadow: false,
    title: 'TORCS AI Overlay',
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWindow.loadFile(path.join(__dirname, '..', 'src', 'index.html'));
}

function createEngineerWindow() {
  engineerWindow = new BrowserWindow({
    ...getEngineerWindowBounds(),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    fullscreenable: false,
    hasShadow: false,
    title: 'TORCS AI Engineer Overlay',
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  engineerWindow.setAlwaysOnTop(true, 'screen-saver');
  engineerWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  engineerWindow.loadFile(path.join(__dirname, '..', 'src', 'engineer.html'));
}

function createSettingsWindow() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }

  settingsWindow = new BrowserWindow({
    width: 920,
    height: 760,
    minWidth: 760,
    minHeight: 620,
    title: 'TORCS AI Overlay Settings',
    backgroundColor: '#111111',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  settingsWindow.loadFile(path.join(__dirname, '..', 'src', 'settings.html'));
  settingsWindow.on('closed', () => {
    settingsWindow = null;
  });
}

function showOverlayWindow() {
  if (!overlayWindow || overlayWindow.isDestroyed()) {
    createOverlayWindow();
    return;
  }

  overlayWindow.show();
  overlayWindow.focus();
}

function showEngineerWindow() {
  if (!engineerWindow || engineerWindow.isDestroyed()) {
    createEngineerWindow();
    return;
  }

  engineerWindow.show();
  engineerWindow.focus();
}

function buildMenu() {
  const template = [
    {
      label: 'TORCS AI Overlay',
      submenu: [
        {
          label: 'Settings',
          click: createSettingsWindow
        },
        {
          label: 'Show Overlay',
          click: showOverlayWindow
        },
        {
          label: 'Hide Overlay',
          click: () => {
            if (overlayWindow) {
              overlayWindow.hide();
            }
          }
        },
        { type: 'separator' },
        {
          label: 'Show Engineer Overlay',
          click: showEngineerWindow
        },
        {
          label: 'Hide Engineer Overlay',
          click: () => {
            if (engineerWindow) {
              engineerWindow.hide();
            }
          }
        },
        { type: 'separator' },
        { role: 'quit' }
      ]
    }
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function toSpeechDispatcherRate(rate) {
  return Math.round(clamp(((Number(rate) || 1) - 1) * 100, -100, 100));
}

function toSpeechDispatcherPitch(pitch) {
  return Math.round(clamp(((Number(pitch) || 1) - 1) * 100, -100, 100));
}

function toSpeechDispatcherVolume(volume) {
  return Math.round(clamp(((Number(volume) || 1) - 1) * 100, -100, 100));
}

function stopNativeSpeech() {
  if (speechProcess && !speechProcess.killed) {
    speechProcess.kill();
  }
  speechProcess = null;
  spawn('spd-say', ['-C'], { stdio: 'ignore' }).on('error', () => {});
}

function speakNative(text, voiceSettings = {}) {
  const spokenText = typeof text === 'string' ? text.trim() : '';
  if (!spokenText) {
    return { ok: false, message: 'No text to speak.' };
  }

  if (speechProcess && !speechProcess.killed) {
    speechProcess.kill();
  }
  speechProcess = null;

  const args = [
    '-P', 'important',
    '-r', String(toSpeechDispatcherRate(voiceSettings.rate)),
    '-p', String(toSpeechDispatcherPitch(voiceSettings.pitch)),
    '-i', String(toSpeechDispatcherVolume(voiceSettings.volume)),
    spokenText
  ];

  speechProcess = spawn('spd-say', args, { stdio: 'ignore', shell: true });
  speechProcess.on('error', () => {
    speechProcess = null;
  });
  speechProcess.on('exit', () => {
    speechProcess = null;
  });

  return { ok: true };
}

app.whenReady().then(() => {
  writeSettings(readSettings());
  buildMenu();
  createOverlayWindow();
  createEngineerWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createOverlayWindow();
      createEngineerWindow();
    }
  });
});

ipcMain.handle('overlay:hide', () => {
  if (overlayWindow) {
    overlayWindow.hide();
  }
});

ipcMain.handle('overlay:open-settings', () => {
  createSettingsWindow();
});

ipcMain.handle('settings:get', () => readSettings());

ipcMain.handle('settings:save', (_event, settings) => {
  const saved = writeSettings(settings);
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send('settings:updated', saved);
  }
  if (engineerWindow && !engineerWindow.isDestroyed()) {
    engineerWindow.webContents.send('settings:updated', saved);
  }
  return saved;
});

ipcMain.handle('voice:speak', (_event, text, voiceSettings) => speakNative(text, voiceSettings));
ipcMain.handle('voice:stop', () => {
  stopNativeSpeech();
  return { ok: true };
});

ipcMain.handle('overlay:resize', (event, contentHeight) => {
  const { y, height: screenH } = screen.getPrimaryDisplay().workArea;
  const newH = Math.max(80, Math.min(Math.ceil(contentHeight) + 24, 400));

  // Commentary window: bottom edge stays put, grows upward.
  if (overlayWindow && !overlayWindow.isDestroyed() && event.sender === overlayWindow.webContents) {
    const bounds = overlayWindow.getBounds();
    overlayWindow.setSize(bounds.width, newH);
    overlayWindow.setPosition(bounds.x, Math.round(y + screenH - newH - 56));
    return;
  }

  // Engineer window: top edge stays put, grows downward (mirror of the above).
  if (engineerWindow && !engineerWindow.isDestroyed() && event.sender === engineerWindow.webContents) {
    const bounds = engineerWindow.getBounds();
    engineerWindow.setSize(bounds.width, newH);
    engineerWindow.setPosition(bounds.x, Math.round(y + 56));
    return;
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
