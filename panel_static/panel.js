"use strict";

const MAX_ENTRIES = 1500;
const HISTORY_KEY = "duality-console-history";
const HISTORY_SIZE = 50;
const WS_CLOSE_NO_ACCESS = 4403;

const state = {
  me: null,
  socket: null,
  retryDelay: 1000,
  commands: [],
  entries: [],
  lastLogId: 0,
  paused: false,
  history: loadHistory(),
  historyIndex: 0,
  nextRequestId: 1,
  pending: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const clock = new Intl.DateTimeFormat("de-DE", {
  timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit", second: "2-digit",
});

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = state.me ? state.me.csrf : "";
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    credentials: "same-origin",
  });
  if (response.status === 401 || response.status === 403) {
    // Abgemeldet oder Staff-Rolle verloren: Die Startseite erklärt, warum.
    window.location.href = "/";
    throw new Error("Kein Zugriff");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Fehler ${response.status}`);
  return data;
}

function setBanner(text) {
  const banner = $("#connection-banner");
  banner.textContent = text || "";
  banner.hidden = !text;
}

/* Navigation */

function showPage() {
  const links = [...document.querySelectorAll("#nav a")].filter((link) => !link.parentElement.hidden);
  const wanted = window.location.hash.slice(1);
  const current = links.find((link) => link.dataset.page === wanted) || links[0];
  if (!current) return;
  for (const link of links) {
    if (link === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll(".page")) {
    section.hidden = section.id !== `page-${current.dataset.page}`;
  }
  document.title = `${current.textContent} · Duality-Panel`;
  if (current.dataset.page === "konsole") scrollLog(true);
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } finally {
    window.location.href = "/";
  }
}

/* Konsole: Log-Anzeige */

function selectedLevels() {
  return new Set([...document.querySelectorAll("input[name=log-level]:checked")].map((box) => box.value));
}

function isVisible(entry, levels, search) {
  if (entry.kind !== "log") return true;
  const level = entry.level === "CRITICAL" ? "ERROR" : entry.level;
  if (!levels.has(level)) return false;
  return !search || `${entry.name} ${entry.message}`.toLowerCase().includes(search);
}

function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

function renderEntry(entry) {
  const item = document.createElement("li");
  item.append(span("time", clock.format(new Date(entry.time * 1000))), " ");
  if (entry.kind === "log") {
    item.className = entry.level;
    item.append(span("lvl", entry.level), " ", span("name", entry.name), " ", entry.message);
  } else if (entry.kind === "command") {
    item.append(span("cmd", `› ${entry.text}`));
  } else {
    item.append(span(entry.ok ? "result-ok" : "result-error", entry.text));
  }
  return item;
}

function scrollLog(force = false) {
  const log = $("#log");
  if (force || !state.paused) log.scrollTop = log.scrollHeight;
}

function renderLog() {
  const levels = selectedLevels();
  const search = $("#log-search").value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  for (const entry of state.entries) {
    if (isVisible(entry, levels, search)) fragment.append(renderEntry(entry));
  }
  $("#log").replaceChildren(fragment);
  scrollLog();
}

function addEntry(entry) {
  state.entries.push(entry);
  if (state.entries.length > MAX_ENTRIES) state.entries.splice(0, state.entries.length - MAX_ENTRIES);
  const log = $("#log");
  if (isVisible(entry, selectedLevels(), $("#log-search").value.trim().toLowerCase())) {
    log.append(renderEntry(entry));
    while (log.childElementCount > MAX_ENTRIES) log.firstElementChild.remove();
    scrollLog();
  }
}

function addResult(ok, text) {
  addEntry({ kind: "result", time: Date.now() / 1000, ok, text });
}

/* Konsole: Verbindung */

function connect() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws?csrf=${encodeURIComponent(state.me.csrf)}`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    state.retryDelay = 1000;
    setBanner(null);
  });
  socket.addEventListener("message", (event) => handleMessage(JSON.parse(event.data)));
  socket.addEventListener("close", (event) => {
    state.socket = null;
    for (const line of state.pending.values()) addResult(false, `Verbindung getrennt, Ergebnis von „${line}“ unbekannt.`);
    state.pending.clear();
    if (event.code === WS_CLOSE_NO_ACCESS) {
      window.location.href = "/";
      return;
    }
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  setBanner(`Verbindung zum Bot getrennt. Neuer Versuch in ${Math.round(state.retryDelay / 1000)} s …`);
  setTimeout(reconnect, state.retryDelay);
  state.retryDelay = Math.min(state.retryDelay * 2, 15000);
}

async function reconnect() {
  // Prüft zuerst Anmeldung und Rechte; nach einem Neustart ist der Bot evtl. noch nicht verbunden.
  try {
    state.me = await api("/api/me");
  } catch (error) {
    scheduleReconnect();
    return;
  }
  connect();
}

function handleMessage(message) {
  if (message.type === "hello") {
    // Nach einem Neustart beginnen die Log-IDs wieder bei 1, deshalb komplett ersetzen.
    const logs = message.logs.map((entry) => ({ kind: "log", ...entry }));
    const own = state.entries.filter((entry) => entry.kind !== "log");
    state.entries = [...logs, ...own].sort((a, b) => a.time - b.time).slice(-MAX_ENTRIES);
    state.lastLogId = logs.length ? logs[logs.length - 1].id : 0;
    state.commands = message.commands;
    $("#command-list").replaceChildren(...state.commands.map((command) => {
      const option = document.createElement("option");
      option.value = command.name;
      option.label = command.description;
      return option;
    }));
    renderLog();
  } else if (message.type === "log") {
    if (message.entry.id <= state.lastLogId) return;
    state.lastLogId = message.entry.id;
    addEntry({ kind: "log", ...message.entry });
  } else if (message.type === "result") {
    state.pending.delete(message.id);
    addResult(message.ok, message.text);
  }
}

/* Konsole: Befehlszeile */

function loadHistory() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(stored) ? stored.slice(-HISTORY_SIZE) : [];
  } catch (error) {
    return [];
  }
}

function remember(line) {
  if (state.history[state.history.length - 1] !== line) state.history.push(line);
  state.history = state.history.slice(-HISTORY_SIZE);
  state.historyIndex = state.history.length;
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
  } catch (error) {
    // Ohne Speicher gibt es eben nur den Verlauf dieser Sitzung.
  }
}

function confirmCommand(text, line) {
  const dialog = $("#confirm-dialog");
  $("#confirm-text").textContent = text;
  $("#confirm-command").textContent = line;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
  });
}

async function submitCommand(event) {
  event.preventDefault();
  const input = $("#command-input");
  const line = input.value.trim();
  if (!line) return;
  const name = line.split(/\s+/)[0].toLowerCase();
  const command = state.commands.find((entry) => entry.name === name);
  let confirmed = false;
  if (command && command.confirm) {
    confirmed = await confirmCommand(command.confirm, line);
    if (!confirmed) {
      input.focus();
      return;
    }
  }
  remember(line);
  input.value = "";
  input.focus();
  addEntry({ kind: "command", time: Date.now() / 1000, text: line });
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    addResult(false, "Keine Verbindung zum Bot. Der Befehl wurde nicht gesendet.");
    return;
  }
  const id = state.nextRequestId++;
  state.pending.set(id, line);
  state.socket.send(JSON.stringify({ type: "command", id, line, confirm: confirmed }));
}

function handleCommandKeys(event) {
  const input = event.target;
  if (event.key === "ArrowUp" && state.historyIndex > 0) {
    event.preventDefault();
    state.historyIndex -= 1;
    input.value = state.history[state.historyIndex];
  } else if (event.key === "ArrowDown" && state.historyIndex < state.history.length) {
    event.preventDefault();
    state.historyIndex += 1;
    input.value = state.history[state.historyIndex] || "";
  } else if (event.key === "Tab" && input.value && !/\s/.test(input.value)) {
    const prefix = input.value.toLowerCase();
    const matches = state.commands.filter((command) => command.name.startsWith(prefix));
    if (matches.length === 0) return;
    event.preventDefault();
    if (matches.length === 1) input.value = `${matches[0].name} `;
    else addResult(true, `Passende Befehle: ${matches.map((command) => command.name).join(", ")}`);
  }
}

function togglePause() {
  state.paused = !state.paused;
  const button = $("#log-pause");
  button.setAttribute("aria-pressed", String(state.paused));
  button.textContent = state.paused ? "Auto-Scroll fortsetzen" : "Auto-Scroll pausieren";
  scrollLog(!state.paused);
}

async function init() {
  state.me = await api("/api/me");
  $("#user-name").textContent = state.me.name;
  $("#user-role").textContent = state.me.role;
  $("#user-avatar").src = state.me.avatar;
  state.historyIndex = state.history.length;
  // Menüpunkte ohne Berechtigung ausblenden; der Server prüft die Rechte trotzdem bei jeder Anfrage.
  for (const link of document.querySelectorAll("#nav a")) {
    link.parentElement.hidden = Number(link.dataset.level) > state.me.level;
  }
  $("#logout").addEventListener("click", logout);
  $("#command-form").addEventListener("submit", submitCommand);
  $("#command-input").addEventListener("keydown", handleCommandKeys);
  $("#log-pause").addEventListener("click", togglePause);
  $("#log-search").addEventListener("input", renderLog);
  for (const box of document.querySelectorAll("input[name=log-level]")) box.addEventListener("change", renderLog);
  window.addEventListener("hashchange", showPage);
  showPage();
  connect();
}

init().catch((error) => setBanner(`Das Panel konnte nicht geladen werden: ${error.message}`));
