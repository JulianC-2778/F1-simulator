const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('torcsOverlay', {
  hide: () => ipcRenderer.invoke('overlay:hide'),
  openSettings: () => ipcRenderer.invoke('overlay:open-settings'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  speak: (text, voiceSettings) => ipcRenderer.invoke('voice:speak', text, voiceSettings),
  stopSpeech: () => ipcRenderer.invoke('voice:stop'),
  onSettingsUpdated: (callback) => {
    ipcRenderer.on('settings:updated', (_event, settings) => callback(settings));
  },
  resizeWindow: (height) => ipcRenderer.invoke('overlay:resize', height)
});
