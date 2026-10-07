const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pywebview", {
  shell: "electron",
  api: {
    subscribeCursor: (callback) => {
      ipcRenderer.on("wisp:cursor", (event, position) => callback(position));
    },
    onHotkey: (callback) => {
      ipcRenderer.on("wisp:hotkey", (event, payload) => callback(payload));
    },
    hotkeys: () => ipcRenderer.invoke("wisp:hotkeys"),
    set_view: (width, height) => ipcRenderer.invoke("wisp:set-view", width, height),
    set_interactive: (interactive) =>
      ipcRenderer.invoke("wisp:set-interactive", interactive),
    move: (dx, dy) => ipcRenderer.invoke("wisp:move", dx, dy),
    toggle_pin: () => ipcRenderer.invoke("wisp:toggle-pin"),
    refresh: () => ipcRenderer.invoke("wisp:refresh"),
    minimize: () => ipcRenderer.invoke("wisp:minimize"),
    close: () => ipcRenderer.invoke("wisp:close"),
  },
});
