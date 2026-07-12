const { app, BrowserWindow, Menu, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

let overlayWindow;
let engineerWindow;
let settingsWindow;
let speechProcess;

// Shared with the Python side via ../../config.json (repo root) -- this is
// the same file config.py reads, so the mainline commentary service's host
// and port only need to be edited in one place instead of drifting out of
// sync between the Electron overlay and the Python backend (see the 8765
// vs 8880 port mismatch this project hit before this file existed).
function readSharedConfig() {
  const defaults = { midware_host: '127.0.0.1', midware_port: 8880 };
  try {
    const raw = fs.readFileSync(path.join(__dirname, '..', '..', 'config.json'), 'utf8');
    return { ...defaults, ...JSON.parse(raw) };
  } catch {
    return defaults;
  }
}

const sharedConfig = readSharedConfig();

const defaultSettings = {
  connection: {
    wsUrl: `ws://${sharedConfig.midware_host}:${sharedConfig.midware_port}/ws`,
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
    speechProcess.superseded = true;
    speechProcess.kill();
  }
  speechProcess = null;
  spawn('spd-say', ['-C'], { stdio: 'ignore' }).on('error', () => {});
}

// Spawns spd-say and reports real completion back to the caller via
// 'voice:speech-ended', so the renderer can advance its sentence queue on
// actual playback end instead of guessing from a word-count timer. Each
// process tracks its own `superseded` flag (set synchronously right before
// it is killed/replaced) so a stale exit from a preempted process never
// fires a spurious "ended" notification for the process that replaced it.
function speakNative(text, voiceSettings = {}, sender = null) {
  const spokenText = typeof text === 'string' ? text.trim() : '';
  if (!spokenText) {
    return { ok: false, message: 'No text to speak.' };
  }

  if (speechProcess && !speechProcess.killed) {
    speechProcess.superseded = true;
    speechProcess.kill();
  }

  const args = [
    '-P', 'important',
    '-r', String(toSpeechDispatcherRate(voiceSettings.rate)),
    '-p', String(toSpeechDispatcherPitch(voiceSettings.pitch)),
    '-i', String(toSpeechDispatcherVolume(voiceSettings.volume)),
    spokenText
  ];

  const proc = spawn('spd-say', args, { stdio: 'ignore', shell: true });
  speechProcess = proc;

  // Only a clean exit (code 0) counts as "actually finished speaking". If
  // spd-say errors out or exits non-zero (e.g. no speech-dispatcher backend
  // configured), don't report success — that would make the renderer's
  // sentence queue race ahead instantly instead of pacing at all. Falling
  // through to the renderer's own timer-based fallback at least keeps each
  // caption on screen for a plausible duration even when no audio can play.
  const notifyEnded = (code) => {
    if (speechProcess === proc) {
      speechProcess = null;
    }
    if (!proc.superseded && code === 0) {
      sender?.send('voice:speech-ended');
    }
  };
  proc.on('error', (err) => {
    console.warn('spd-say failed to start:', err.message);
    notifyEnded(-1);
  });
  proc.on('exit', (code) => {
    if (code !== 0) {
      console.warn(`spd-say exited with code ${code} (no speech-dispatcher backend? check "spd-say test" manually)`);
    }
    notifyEnded(code);
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

ipcMain.handle('voice:speak', (event, text, voiceSettings) => speakNative(text, voiceSettings, event.sender));
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
