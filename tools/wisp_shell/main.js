// Electron shell that hosts the Wisp widget as a transparent desktop overlay.
//
// Started by tools/antigravity_viewer.py (open_electron_window), which passes
// the widget URL in WISP_URL. Other environment variables:
//   WISP_HOTKEY_ASK, WISP_HOTKEY_ASK_PROMPT  global hotkeys (accelerator syntax)
//   WISP_CANVAS_W, WISP_CANVAS_H             transparent canvas size in pixels
// Diagnostics (all write to the shell log in the OS temp directory):
//   WISP_OPAQUE=1     opaque window resized per view instead of a fixed canvas
//   WISP_DEBUG_BG     background colour of the opaque window
//   WISP_NUDGE=1      force two repaints after load (compositor glitches)
//   WISP_DIAG=1       log page errors and creature state 3 s after load
//   WISP_CAPTURE=1    save window screenshots to the temp directory at 4 s and 9 s
//   WISP_DEBUG_PORT   open a Chromium remote-debugging port (see below)

const { app, BrowserWindow, globalShortcut, ipcMain, screen, session } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");

// A positive integer from the environment, or `fallback` when unset or invalid.
function envInt(name, fallback) {
  const value = parseInt(process.env[name] || "", 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

const WIDGET_URL = process.env.WISP_URL || "http://127.0.0.1:48477/";
const MARGIN = 18;
const LOG = path.join(os.tmpdir(), "wisp-shell.log");
const LOG_MAX_BYTES = 512 * 1024; // rotated in place; the shell log is diagnostics only
// Fixed transparent canvas. It must hold the widget's tallest footprint (SIZES in
// antigravity_viewer.html): 52 + 384 + 44 wide; the settings view with the
// perched owl and its halo, the dock and shadow clearance is 624 tall.
const CANVAS_W = envInt("WISP_CANVAS_W", 480);
const CANVAS_H = envInt("WISP_CANVAS_H", 624);
// Smallest window the opaque debug shell accepts from the page.
const MIN_VIEW_W = 200;
const MIN_VIEW_H = 160;
const HOTKEY_ASK = process.env.WISP_HOTKEY_ASK || "Control+Alt+Q";
const HOTKEY_ASK_PROMPT = process.env.WISP_HOTKEY_ASK_PROMPT || "Control+Alt+E";
const CURSOR_FEED_MS = 33; // ~30 Hz is plenty for hover hit-testing
const OPAQUE = process.env.WISP_OPAQUE === "1";
const NUDGE = process.env.WISP_NUDGE === "1";
const ALLOWED_ORIGIN = (() => {
  try {
    return new URL(WIDGET_URL).origin;
  } catch (err) {
    return ""; // an invalid WISP_URL leaves every navigation and IPC call denied
  }
})();

// True when `url` belongs to the local Wisp origin the widget is served from.
function isWidgetUrl(url) {
  return !!ALLOWED_ORIGIN && typeof url === "string" &&
    (url === ALLOWED_ORIGIN || url.startsWith(ALLOWED_ORIGIN + "/"));
}

// IPC is honoured only for the widget page itself.
function fromWidget(event) {
  return !!(event && event.senderFrame && isWidgetUrl(event.senderFrame.url));
}

let win = null;
let userPositioned = false;
let cursorTimer = null;

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}
app.on("second-instance", () => {
  if (win) win.showInactive();
});

// WISP_DEBUG_PORT opens a Chromium DevTools protocol port that grants full
// renderer control. It exists for explicit debugging sessions only; never
// enable it while untrusted content can reach the machine.
if (process.env.WISP_DEBUG_PORT) {
  app.commandLine.appendSwitch(
    "remote-debugging-port",
    String(process.env.WISP_DEBUG_PORT)
  );
}

const hotkeyStatus = { quick: HOTKEY_ASK, prompt: HOTKEY_ASK_PROMPT, quick_ok: false, prompt_ok: false };

function registerHotkeys() {
  try {
    hotkeyStatus.quick_ok = globalShortcut.register(HOTKEY_ASK, () => {
      if (win) win.webContents.send("wisp:hotkey", { mode: "quick" });
    });
    hotkeyStatus.prompt_ok = globalShortcut.register(HOTKEY_ASK_PROMPT, () => {
      if (!win) return;
      win.showInactive();
      win.webContents.send("wisp:hotkey", { mode: "prompt" });
    });
  } catch (err) {
    log("hotkey registration failed: " + err);
  }
  log("hotkeys " + JSON.stringify(hotkeyStatus));
}

app.on("will-quit", () => {
  try {
    globalShortcut.unregisterAll();
  } catch (err) {
    /* best effort */
  }
});

function log(message) {
  try {
    try {
      const stat = fs.statSync(LOG);
      if (stat.size > LOG_MAX_BYTES) fs.writeFileSync(LOG, "");
    } catch (err) {
      /* missing file is fine */
    }
    fs.appendFileSync(LOG, new Date().toISOString() + " " + message + "\n");
  } catch (err) {
    /* logging is best effort */
  }
}

function startCursorFeed() {
  if (cursorTimer) return;
  let last = { x: -9999, y: -9999 };
  cursorTimer = setInterval(() => {
    if (!win || win.isDestroyed() || !win.isVisible()) return;
    try {
      const point = screen.getCursorScreenPoint();
      const [wx, wy] = win.getPosition();
      const rel = { x: point.x - wx, y: point.y - wy };
      if (rel.x !== last.x || rel.y !== last.y) {
        last = rel;
        win.webContents.send("wisp:cursor", rel);
      }
    } catch (err) {
      /* cursor feed is best effort */
    }
  }, CURSOR_FEED_MS);
}

function bottomRight(width, height) {
  const area = screen.getPrimaryDisplay().workArea;
  return {
    x: Math.max(area.x, area.x + area.width - width - MARGIN),
    y: Math.max(area.y, area.y + area.height - height - MARGIN),
  };
}

function atAutoPosition() {
  if (!win) return true;
  const [width, height] = win.getSize();
  const expected = bottomRight(width, height);
  const [x, y] = win.getPosition();
  return Math.abs(x - expected.x) <= 2 && Math.abs(y - expected.y) <= 2;
}

function createWindow() {
  const [width, height] = [CANVAS_W, CANVAS_H];
  const position = bottomRight(width, height);
  win = new BrowserWindow({
    width,
    height,
    x: position.x,
    y: position.y,
    frame: false,
    transparent: !OPAQUE,
    resizable: false,
    hasShadow: false,
    alwaysOnTop: true,
    backgroundColor: OPAQUE
      ? process.env.WISP_DEBUG_BG || "#0b0f1a"
      : "#00000000",
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
    },
  });
  // Navigation policy: the widget is a view onto the local Wisp origin only.
  // Any navigation attempt away from it (or any popup/window.open) is denied.
  win.webContents.on("will-navigate", (event, target) => {
    if (!isWidgetUrl(target)) {
      log("navigation denied: " + target);
      event.preventDefault();
    }
  });
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    log("window-open denied: " + target);
    return { action: "deny" };
  });
  log("window created " + WIDGET_URL);
  if (!OPAQUE) {
    try {
      win.setIgnoreMouseEvents(true, { forward: true });
      log("click-through armed");
    } catch (err) {
      log("setIgnoreMouseEvents failed: " + err);
    }
  }
  const nudge = () => {
    try {
      const [w, h] = win.getSize();
      win.setBounds({ width: w + 2, height: h + 2 });
      setTimeout(() => win.setBounds({ width: w, height: h }), 60);
      log("nudged repaint");
    } catch (err) {
      log("nudge failed " + err);
    }
  };
  win.webContents.on("did-finish-load", () => {
    log("did-finish-load");
    if (NUDGE) {
      setTimeout(nudge, 900);
      setTimeout(nudge, 2200);
    }
    if (process.env.WISP_DIAG === "1") {
      setTimeout(() => {
        win.webContents
          .executeJavaScript(
            "JSON.stringify({ errors: window.__wispErrors || [], imgs: document.querySelectorAll('.creature img').length, on: document.querySelectorAll('.creature img.on').length, mode: document.getElementById('app') ? document.getElementById('app').dataset.mode : 'no-app', bodyClass: document.body.className })"
          )
          .then((result) => log("diag " + result))
          .catch((err) => log("diag failed " + err));
      }, 3000);
    }
    if (process.env.WISP_CAPTURE === "1") {
      const shoot = (tag) =>
        win.webContents
          .capturePage()
          .then((image) =>
            fs.writeFileSync(
              path.join(os.tmpdir(), "wisp-shell-capture-" + tag + ".png"),
              image.toPNG()
            )
          )
          .then(() => log("captured " + tag))
          .catch((err) => log("capture failed " + err));
      setTimeout(() => shoot("4s"), 4000);
      setTimeout(() => shoot("9s"), 9000);
    }
  });
  win.webContents.on("did-fail-load", (event, code, description, url) =>
    log("did-fail-load " + code + " " + description + " " + url)
  );
  win.webContents.on("render-process-gone", (event, details) =>
    log("render-process-gone " + JSON.stringify(details))
  );
  win.webContents.on("console-message", (event, ...rest) => {
    const details =
      event && event.message !== undefined
        ? event
        : { level: rest[0], message: rest[1], line: rest[2], sourceId: rest[3] };
    log("console[" + details.level + "] " + details.message + " (" + (details.sourceId || "") + ":" + (details.line || 0) + ")");
  });
  win.loadURL(WIDGET_URL).catch((err) => log("loadURL failed " + err));
  win.on("closed", () => {
    log("window closed");
    win = null;
  });
  startCursorFeed();
}

ipcMain.handle("wisp:set-view", (event, width, height) => {
  if (!win || !fromWidget(event)) return false;
  if (!Number.isFinite(Number(width)) || !Number.isFinite(Number(height))) {
    return false; // NaN from a misbehaving renderer must not reach setBounds
  }
  const w = Math.max(MIN_VIEW_W, Math.round(width));
  const h = Math.max(MIN_VIEW_H, Math.round(height));
  // The transparent shell keeps a fixed canvas and the page sizes its own
  // widget inside it; only the opaque debug window is resized per mode.
  if (!OPAQUE) return true;
  if (!userPositioned && !atAutoPosition()) userPositioned = true;
  const [currentWidth, currentHeight] = win.getSize();
  const [currentX, currentY] = win.getPosition();
  const area = screen.getPrimaryDisplay().workArea;
  let [x, y] = [currentX, currentY];
  if (!userPositioned) {
    ({ x, y } = bottomRight(w, h));
  } else {
    x = Math.max(area.x, Math.min(x, area.x + area.width - w));
    y = Math.max(area.y, Math.min(y, area.y + area.height - h));
  }
  if (w !== currentWidth || h !== currentHeight) {
    win.setBounds({ x, y, width: w, height: h });
  } else if (x !== currentX || y !== currentY) {
    win.setPosition(x, y);
  }
  return true;
});

ipcMain.handle("wisp:move", (event, dx, dy) => {
  if (!win || !fromWidget(event)) return [0, 0];
  // Renderer-supplied deltas are coerced and the result is clamped to the
  // work area, so a misbehaving page cannot fling the always-on-top window
  // off-screen.
  const stepX = Number(dx);
  const stepY = Number(dy);
  if (!Number.isFinite(stepX) || !Number.isFinite(stepY)) return win.getPosition();
  const area = screen.getPrimaryDisplay().workArea;
  const [currentX, currentY] = win.getPosition();
  const [width, height] = win.getSize();
  const targetX = Math.max(
    area.x,
    Math.min(currentX + Math.round(stepX), area.x + area.width - width)
  );
  const targetY = Math.max(
    area.y,
    Math.min(currentY + Math.round(stepY), area.y + area.height - height)
  );
  win.setPosition(targetX, targetY);
  userPositioned = true;
  return win.getPosition();
});

ipcMain.handle("wisp:toggle-pin", (event) => {
  if (!win || !fromWidget(event)) return false;
  win.setAlwaysOnTop(!win.isAlwaysOnTop());
  return win.isAlwaysOnTop();
});

ipcMain.handle("wisp:is-pinned", (event) => (win && fromWidget(event) ? win.isAlwaysOnTop() : false));

ipcMain.handle("wisp:set-interactive", (event, interactive) => {
  if (!win || !fromWidget(event)) return false;
  try {
    win.setIgnoreMouseEvents(!interactive, { forward: true });
    return true;
  } catch (err) {
    log("set-interactive failed: " + err);
    return false;
  }
});

ipcMain.handle("wisp:minimize", (event) => {
  if (win && fromWidget(event)) win.minimize();
});

ipcMain.handle("wisp:hotkeys", (event) => (fromWidget(event) ? hotkeyStatus : null));

ipcMain.handle("wisp:refresh", (event) => {
  if (!fromWidget(event)) return false;
  if (win) {
    try {
      win.webContents.invalidate();
    } catch (err) {
      /* best effort */
    }
  }
  return true;
});

ipcMain.handle("wisp:close", (event) => {
  if (win && fromWidget(event)) win.close();
});

if (gotSingleInstanceLock) {
  app.whenReady().then(() => {
    // The widget needs no permission beyond writing to the clipboard (its
    // copy buttons); deny everything else, from every origin.
    session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
      const requester = (details && details.requestingUrl) || contents.getURL();
      callback(permission === "clipboard-sanitized-write" && isWidgetUrl(requester));
    });
    createWindow();
    registerHotkeys();
  });
}
app.on("window-all-closed", () => {
  if (cursorTimer) clearInterval(cursorTimer);
  app.quit();
});
