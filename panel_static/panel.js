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
  status: null,
  voice: [],
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
  if (response.status === 401) {
    // Abgemeldet: Die Startseite zeigt die Anmeldung.
    window.location.href = "/";
    throw new Error("Bitte melde dich an.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Fehler ${response.status}`);
    error.status = response.status;
    throw error;
  }
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
  const loader = pageLoaders[current.dataset.page];
  if (loader) loader().catch((error) => setBanner(`Laden fehlgeschlagen: ${error.message}`));
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } finally {
    window.location.href = "/";
  }
}

/* Dashboard */

const dateTime = new Intl.DateTimeFormat("de-DE", {
  timeZone: "Europe/Berlin", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});

function formatTime(seconds) {
  return seconds ? `${dateTime.format(new Date(seconds * 1000))} Uhr` : "–";
}

function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes % 60} min`;
  return `${minutes} min`;
}

function pill(text, kind) {
  return span(`pill ${kind}`, text);
}

function fillList(list, rows) {
  const items = [];
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.append(value ?? "–");
    items.push(term, detail);
  }
  list.replaceChildren(...items);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toLocaleString("de-DE", { maximumFractionDigits: 1 })} KB`;
  return `${(bytes / 1024 / 1024).toLocaleString("de-DE", { maximumFractionDigits: 1 })} MB`;
}

function renderUptime() {
  const element = $("#dash-uptime");
  if (element && state.status) element.textContent = formatDuration(Date.now() / 1000 - state.status.bot.started_at);
}

function listItem(...parts) {
  const item = document.createElement("li");
  item.append(...parts);
  return item;
}

function renderAlerts(alerts) {
  $("#dash-alert-channel").textContent = `Warnungen gehen an: ${alerts.channel}`;
  $("#dash-alerts-active").replaceChildren(...(alerts.active.length
    ? alerts.active.map((problem) => listItem(
      pill("Aktiv", "bad"), problem.text, span("muted small", `seit ${formatTime(problem.since)}`),
    ))
    : [listItem(pill("OK", "ok"), "Keine aktiven Probleme")]));
  $("#dash-alerts-recent").replaceChildren(...(alerts.recent.length
    ? alerts.recent.map((message) => listItem(
      message.kind === "alert" ? pill("Warnung", "bad") : pill("Behoben", "ok"),
      message.text,
      span("muted small", `${formatTime(message.time)} · ${message.delivered}`),
    ))
    : [listItem(span("muted", "Noch keine Meldungen seit dem Start."))]));
}

function renderDashboard(status) {
  state.status = status;
  const { bot, guild, henrikdev: api } = status;
  const uptime = span("", "");
  uptime.id = "dash-uptime";
  fillList($("#dash-bot"), [
    ["Status", bot.connected ? pill("Verbunden", "ok") : pill("Getrennt", "bad")],
    ["Latenz", bot.latency_ms !== null ? `${bot.latency_ms} ms` : "–"],
    ["Läuft seit", uptime],
    ["Angemeldet als", bot.user],
    ["Python", bot.python],
    ["discord.py", bot.discord_py],
    ["Commit", span("mono", bot.commit)],
  ]);
  renderUptime();
  fillList($("#dash-guild"), guild ? [
    ["Name", guild.name],
    ["Mitglieder", String(guild.members)],
    ["Spieler", String(guild.players)],
    ["Bewerber", String(guild.applicants)],
    ["Staff", String(guild.staff)],
  ] : [["Status", "Server nicht verfügbar"]]);
  let apiState = pill("Aus", "warn");
  if (api.enabled) apiState = api.rate_limited ? pill("Rate-Limit", "warn") : pill("Aktiv", api.failures_in_row ? "warn" : "ok");
  fillList($("#dash-api"), [
    ["Status", apiState],
    ["Rate-Limit bis", api.rate_limited ? formatTime(api.rate_limited_until) : "–"],
    ["Fehler in Folge", String(api.failures_in_row)],
    ["Letzter Fehler", api.last_error ? `${api.last_error} (${formatTime(api.last_error_at)})` : "–"],
  ]);
  $("#dash-tasks").replaceChildren(...status.tasks.map((task) => {
    const row = document.createElement("tr");
    const cells = [
      task.label,
      task.running ? pill("Läuft", "ok") : pill("Gestoppt", "bad"),
      formatTime(task.last_run),
      formatTime(task.next_run),
      task.last_error ? `${task.last_error} (${formatTime(task.last_error_at)})` : "–",
    ];
    for (const value of cells) {
      const cell = document.createElement("td");
      cell.append(value);
      row.append(cell);
    }
    return row;
  }));
  const backup = status.backup;
  fillList($("#dash-backup"), [
    ["Letztes", backup.last ? formatTime(backup.last.created) : "noch keins"],
    ["Größe", backup.last ? formatSize(backup.last.size) : "–"],
    ["Vorhanden", String(backup.count)],
    ["Letzter Fehler", backup.last_error ? pill("Fehlgeschlagen", "bad") : "–"],
  ]);
  renderAlerts(status.alerts);
  $("#dash-updated").textContent = `Zuletzt aktualisiert: ${clock.format(new Date(status.time * 1000))} Uhr (alle 5 Sekunden)`;
}

/* Seiten mit Daten aus /api */

function tableRow(...cells) {
  const row = document.createElement("tr");
  for (const value of cells) {
    const cell = document.createElement("td");
    cell.append(value ?? "–");
    row.append(cell);
  }
  return row;
}

function emptyRow(columns, text) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = columns;
  cell.className = "empty";
  cell.textContent = text;
  row.append(cell);
  return row;
}

async function loadBackups() {
  const data = await api("/api/backups");
  $("#backup-list").replaceChildren(...(data.backups.length ? data.backups.map((backup) => {
    const link = document.createElement("a");
    link.className = "button small";
    link.href = `/api/backups/${encodeURIComponent(backup.name)}`;
    link.download = backup.name;
    link.textContent = "Herunterladen";
    link.setAttribute("aria-label", `${backup.name} herunterladen`);
    return tableRow(span("mono", backup.name), formatTime(backup.created), formatSize(backup.size), link);
  }) : [emptyRow(4, "Noch keine Backups vorhanden.")]));
  $("#backup-message").textContent = data.last_error ? `Letzter Fehler: ${data.last_error}` : "";
}

async function createBackup() {
  const button = $("#backup-create");
  button.disabled = true;
  $("#backup-message").textContent = "Backup läuft …";
  try {
    const backup = await api("/api/backups", { method: "POST" });
    await loadBackups();
    $("#backup-message").textContent = `Erstellt: ${backup.name} (${formatSize(backup.size)})`;
  } catch (error) {
    $("#backup-message").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function personCell(person) {
  if (!person) return "–";
  const wrapper = span("person", "");
  if (person.avatar) {
    const avatar = document.createElement("img");
    avatar.src = person.avatar;
    avatar.alt = "";
    avatar.width = 24;
    avatar.height = 24;
    wrapper.append(avatar);
  }
  wrapper.append(person.on_server ? person.name : span("muted", person.name));
  return wrapper;
}

function discordLink(url, label = "In Discord öffnen") {
  if (!url) return "–";
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  return link;
}

const APPLICATION_PILLS = { pending: "warn", accepted: "ok", declined: "bad", left: "neutral" };

async function loadApplications() {
  const status = $("#application-filter").value;
  const data = await api(`/api/applications?status=${encodeURIComponent(status)}`);
  const rows = data.applications.map((application) => tableRow(
    personCell(application.applicant),
    pill(application.status_label, APPLICATION_PILLS[application.status] || "neutral"),
    formatTime(application.submitted_at),
    `👍 ${application.votes.yes} · 👎 ${application.votes.no}`,
    application.decided_by ? personCell(application.decided_by) : "–",
    discordLink(application.url),
  ));
  $("#application-list").replaceChildren(...(rows.length ? rows : [emptyRow(6, "Keine Bewerbungen mit diesem Status.")]));
  $("#application-count").textContent = `${rows.length} ${rows.length === 1 ? "Bewerbung" : "Bewerbungen"}`;
}

async function startNewSeason() {
  const confirmed = await confirmAction(
    "Alle mit der Rolle „Bewerber“, deren Bewerbung nicht mehr offen ist, verlieren diese Rolle und können sich über #bewerbung erneut bewerben. Offene Bewerbungen bleiben unverändert.",
    null,
    "Neue Saison starten",
  );
  if (!confirmed) return;
  const button = $("#new-season");
  button.disabled = true;
  try {
    const result = await api("/api/applications/new-season", { method: "POST", body: { confirm: true } });
    $("#application-count").textContent = `Neue Saison gestartet: ${result.reopened} Personen können sich wieder bewerben.`;
  } catch (error) {
    $("#application-count").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

const pageLoaders = { backups: loadBackups, bewerbungen: loadApplications };

function renderVoice(channels) {
  state.voice = channels;
  const hideEmpty = $("#voice-hide-empty").checked;
  const people = channels.reduce((sum, channel) => sum + channel.members.length, 0);
  const active = channels.filter((channel) => channel.members.length).length;
  $("#voice-summary").textContent = `${people} ${people === 1 ? "Person" : "Personen"} in ${active} von ${channels.length} Kanälen`;
  const items = [];
  let category;
  for (const channel of channels) {
    if (hideEmpty && !channel.members.length) continue;
    if (channel.category !== category) {
      category = channel.category;
      const heading = document.createElement("h2");
      heading.className = "voice-category";
      heading.textContent = category || "Ohne Kategorie";
      items.push(heading);
    }
    const card = document.createElement("article");
    card.className = `card voice-channel${channel.members.length ? "" : " empty-channel"}`;
    const title = document.createElement("h2");
    title.textContent = `🔊 ${channel.name}`;
    card.append(title);
    if (channel.temporary) card.append(pill(`Temporär · gehört ${channel.owner}`, "warn"));
    const list = document.createElement("ul");
    for (const member of channel.members) {
      const avatar = document.createElement("img");
      avatar.src = member.avatar;
      avatar.alt = "";
      avatar.width = 24;
      avatar.height = 24;
      const flags = span("voice-flags", "");
      if (member.muted) flags.append(pill("stumm", "warn"));
      if (member.deafened) flags.append(pill("taub", "warn"));
      if (member.streaming) flags.append(pill("streamt", "bad"));
      if (member.video) flags.append(pill("Kamera", "ok"));
      list.append(listItem(avatar, member.name, flags));
    }
    if (!channel.members.length) list.append(listItem(span("muted small", "leer")));
    card.append(list);
    items.push(card);
  }
  $("#voice-list").replaceChildren(...items);
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
    if (error.status === 403) {
      window.location.href = "/";
      return;
    }
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
    renderDashboard(message.status);
    renderVoice(message.voice);
  } else if (message.type === "status") {
    renderDashboard(message.status);
  } else if (message.type === "voice") {
    renderVoice(message.channels);
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

function confirmAction(text, detail, okLabel = "Ausführen") {
  const dialog = $("#confirm-dialog");
  $("#confirm-text").textContent = text;
  $("#confirm-command").textContent = detail || "";
  $("#confirm-command").hidden = !detail;
  $("#confirm-ok").textContent = okLabel;
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
    confirmed = await confirmAction(command.confirm, line);
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
  for (const element of document.querySelectorAll("[data-level]")) {
    const target = element.closest("#nav") ? element.parentElement : element;
    target.hidden = Number(element.dataset.level) > state.me.level;
  }
  $("#logout").addEventListener("click", logout);
  $("#command-form").addEventListener("submit", submitCommand);
  $("#command-input").addEventListener("keydown", handleCommandKeys);
  $("#log-pause").addEventListener("click", togglePause);
  $("#backup-create").addEventListener("click", createBackup);
  $("#voice-hide-empty").addEventListener("change", () => renderVoice(state.voice));
  $("#application-filter").addEventListener("change", () => loadApplications().catch((error) => setBanner(error.message)));
  $("#new-season").addEventListener("click", startNewSeason);
  $("#log-search").addEventListener("input", renderLog);
  for (const box of document.querySelectorAll("input[name=log-level]")) box.addEventListener("change", renderLog);
  window.addEventListener("hashchange", showPage);
  showPage();
  setInterval(renderUptime, 1000);
  connect();
}

init().catch((error) => setBanner(`Das Panel konnte nicht geladen werden: ${error.message}`));
