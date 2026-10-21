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
  players: [],
  events: [],
  eventsView: "list",
  calendarMonth: null,
  editingEvent: null,
  attendance: null,
  attendanceSort: { key: "name", ascending: true },
  stats: null,
  statsSort: { key: "name", ascending: true },
  statsUser: null,
  texts: [],
  announcementData: null,
  editingAnnouncement: null,
  privacyUser: null,
  setupPreview: null,
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
  const links = [...document.querySelectorAll("#nav a")].filter((link) => !link.closest("[hidden]"));
  const wanted = window.location.hash.slice(1);
  const current = links.find((link) => link.dataset.page === wanted) || links[0];
  if (!current) return;
  for (const link of links) {
    link.classList.toggle("menu-active", link === current);
    if (link === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll(".page")) {
    section.hidden = section.id !== `page-${current.dataset.page}`;
  }
  document.title = `${current.textContent} · Duality-Panel`;
  // Auf dem Handy ist die Navigation eine seitlich scrollende Leiste: aktiven Punkt sichtbar machen.
  current.scrollIntoView({ block: "nearest", inline: "nearest" });
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

const BADGES = { ok: "badge-success", warn: "badge-warning", bad: "badge-error", neutral: "badge-quiet" };

function pill(text, kind) {
  return span(`badge badge-sm ${BADGES[kind] || BADGES.neutral}`, text);
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
    link.className = "btn btn-sm";
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

function renderPlayers() {
  const search = $("#player-search").value.trim().toLowerCase();
  const rows = state.players
    .filter((player) => !search || `${player.user.name} ${player.riot_id || ""}`.toLowerCase().includes(search))
    .map((player) => {
      let action = "";
      if (state.me.level >= 2 && player.riot_id && player.user.on_server) {
        action = document.createElement("button");
        action.type = "button";
        action.className = "btn btn-sm";
        action.textContent = "Rang abgleichen";
        action.setAttribute("aria-label", `Rang von ${player.user.name} jetzt abgleichen`);
        action.addEventListener("click", () => syncPlayerRank(player, action));
      }
      return tableRow(
        personCell(player.user),
        pill(player.role, player.role === "Spieler" ? "ok" : "neutral"),
        player.riot_id ? span("mono", player.riot_id) : "nicht verknüpft",
        player.rank_tier || "–",
        formatTime(player.rank_synced_at),
        player.main_role || "–",
        player.playable_roles || "–",
        action,
      );
    });
  $("#player-list").replaceChildren(...(rows.length ? rows : [emptyRow(8, "Keine passenden Profile.")]));
}

async function loadPlayers() {
  const filter = $("#players-only").checked ? "players" : "all";
  state.players = (await api(`/api/players?filter=${filter}`)).players;
  renderPlayers();
}

async function syncPlayerRank(player, button) {
  button.disabled = true;
  $("#player-message").textContent = `Gleiche den Rang von ${player.user.name} ab …`;
  try {
    const result = await api(`/api/players/${player.user.id}/sync-rank`, { method: "POST" });
    $("#player-message").textContent = result.message;
    await loadPlayers();
  } catch (error) {
    $("#player-message").textContent = error.message;
    button.disabled = false;
  }
}

/* Termine */

const berlinParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function berlinDate(seconds) {
  const parts = Object.fromEntries(berlinParts.formatToParts(new Date(seconds * 1000)).map((part) => [part.type, part.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}`, year: Number(parts.year), month: Number(parts.month) - 1 };
}

const weekday = new Intl.DateTimeFormat("de-DE", { timeZone: "Europe/Berlin", weekday: "short" });

function eventWhen(seconds) {
  return `${weekday.format(new Date(seconds * 1000))}, ${formatTime(seconds)}`;
}

function rsvpDetails(event) {
  const wrapper = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `✅ ${event.rsvps.going.length} · 🤔 ${event.rsvps.maybe.length} · ❌ ${event.rsvps.declined.length}`;
  wrapper.append(summary);
  for (const [choice, label] of [["going", "Dabei"], ["maybe", "Vielleicht"], ["declined", "Kann nicht"]]) {
    const heading = span("small muted", label);
    const list = document.createElement("ul");
    for (const person of event.rsvps[choice]) list.append(listItem(person.name));
    if (!event.rsvps[choice].length) list.append(listItem(span("muted", "niemand")));
    wrapper.append(heading, list);
  }
  return wrapper;
}

function reminderText(event) {
  const sent = Object.entries(event.reminders).map(([key, at]) => `${key} ✓ (${formatTime(at)})`);
  return sent.length ? sent.join(" · ") : "–";
}

function eventActions(event) {
  const actions = span("row-actions", "");
  actions.append(discordLink(event.url, "Discord"));
  if (event.upcoming && !event.cancelled_at) {
    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "btn btn-sm";
    editButton.textContent = "Bearbeiten";
    editButton.setAttribute("aria-label", `${event.title} bearbeiten`);
    editButton.addEventListener("click", () => openEventDialog(event));
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "btn btn-sm btn-error btn-outline";
    cancelButton.textContent = "Absagen";
    cancelButton.setAttribute("aria-label", `${event.title} absagen`);
    cancelButton.addEventListener("click", () => cancelEvent(event));
    actions.append(editButton, cancelButton);
  }
  return actions;
}

function renderEventList() {
  const rows = state.events.map((event) => {
    const title = span(event.cancelled_at ? "event-cancelled" : "", event.title);
    const cell = span("", "");
    cell.append(title);
    if (event.cancelled_at) cell.append(" ", pill("abgesagt", "bad"));
    return tableRow(eventWhen(event.starts_at), cell, rsvpDetails(event), reminderText(event), eventActions(event));
  });
  $("#events-list").replaceChildren(...(rows.length ? rows : [emptyRow(5, "Keine Termine in diesem Zeitraum.")]));
}

function renderCalendar() {
  const now = berlinDate(Date.now() / 1000);
  if (!state.calendarMonth) state.calendarMonth = { year: now.year, month: now.month };
  const { year, month } = state.calendarMonth;
  $("#calendar-title").textContent = new Intl.DateTimeFormat("de-DE", { month: "long", year: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(year, month, 1)));
  const byDay = {};
  for (const event of state.events) (byDay[berlinDate(event.starts_at).day] ||= []).push(event);
  const first = new Date(Date.UTC(year, month, 1));
  const start = new Date(first);
  start.setUTCDate(1 - ((first.getUTCDay() + 6) % 7));
  const rows = [];
  for (let week = 0; week < 6; week += 1) {
    const row = document.createElement("tr");
    for (let day = 0; day < 7; day += 1) {
      const date = new Date(start);
      date.setUTCDate(start.getUTCDate() + week * 7 + day);
      const key = date.toISOString().slice(0, 10);
      const cell = document.createElement("td");
      if (date.getUTCMonth() !== month) cell.classList.add("outside");
      if (key === now.day) cell.classList.add("today");
      cell.append(span("day-number", String(date.getUTCDate())));
      for (const event of byDay[key] || []) {
        const entry = span(`calendar-event${event.cancelled_at ? " event-cancelled" : ""}`, `${berlinDate(event.starts_at).time} ${event.title}`);
        entry.title = `${event.title} · ✅ ${event.rsvps.going.length} · 🤔 ${event.rsvps.maybe.length} · ❌ ${event.rsvps.declined.length}`;
        cell.append(entry);
      }
      row.append(cell);
    }
    rows.push(row);
    const nextWeek = new Date(start);
    nextWeek.setUTCDate(start.getUTCDate() + (week + 1) * 7);
    if (nextWeek.getUTCMonth() !== month && week >= 3) break;
  }
  $("#calendar-body").replaceChildren(...rows);
}

function showEventsView(view) {
  state.eventsView = view;
  $("#events-view-list").setAttribute("aria-pressed", String(view === "list"));
  $("#events-view-calendar").setAttribute("aria-pressed", String(view === "calendar"));
  $("#events-list-view").hidden = view !== "list";
  $("#events-calendar-view").hidden = view !== "calendar";
  // Der Kalender zeigt immer alle Termine, die Liste den gewählten Zeitraum.
  $("#events-range").disabled = view === "calendar";
  loadEvents().catch((error) => setBanner(error.message));
}

async function loadEvents() {
  const range = state.eventsView === "calendar" ? "all" : $("#events-range").value;
  state.events = (await api(`/api/events?range=${range}`)).events;
  if (state.eventsView === "calendar") renderCalendar();
  else renderEventList();
}

function berlinInputValue(seconds) {
  const parts = berlinDate(seconds);
  return `${parts.day}T${parts.time}`;
}

function openEventDialog(event = null) {
  state.editingEvent = event;
  $("#event-dialog-title").textContent = event ? "Termin bearbeiten" : "Neuer Termin";
  $("#event-save").textContent = event ? "Speichern" : "Veröffentlichen";
  $("#event-title").value = event ? event.title : "";
  $("#event-details").value = event ? event.details : "";
  $("#event-start").value = event ? berlinInputValue(event.starts_at) : "";
  $("#event-start").min = berlinInputValue(Date.now() / 1000);
  $("#event-notify-row").hidden = !event;
  $("#event-notify").checked = false;
  $("#event-error").textContent = "";
  $("#event-dialog").showModal();
}

async function saveEvent(submitEvent) {
  submitEvent.preventDefault();
  const button = $("#event-save");
  button.disabled = true;
  const body = {
    title: $("#event-title").value,
    starts_at: $("#event-start").value,
    details: $("#event-details").value,
    notify: $("#event-notify").checked,
  };
  try {
    const path = state.editingEvent ? `/api/events/${state.editingEvent.id}` : "/api/events";
    const result = await api(path, { method: "POST", body });
    $("#event-dialog").close();
    $("#events-message").textContent = result.message;
    await loadEvents();
  } catch (error) {
    $("#event-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function cancelEvent(event) {
  const confirmed = await confirmAction(
    `„${event.title}“ am ${formatTime(event.starts_at)} wird in Discord als abgesagt markiert. Die Buttons werden deaktiviert und es gehen keine Erinnerungen mehr raus.`,
    null,
    "Termin absagen",
    "Zusagen über die Absage benachrichtigen (erwähnt nur sie)",
  );
  if (!confirmed) return;
  try {
    const result = await api(`/api/events/${event.id}/cancel`, {
      method: "POST", body: { confirm: true, notify: $("#confirm-option").checked },
    });
    $("#events-message").textContent = result.message;
    await loadEvents();
  } catch (error) {
    $("#events-message").textContent = error.message;
  }
}

function moveCalendar(step) {
  const { year, month } = state.calendarMonth;
  const next = new Date(Date.UTC(year, month + step, 1));
  state.calendarMonth = { year: next.getUTCFullYear(), month: next.getUTCMonth() };
  renderCalendar();
}

/* Anwesenheit */

const CHOICE_LABELS = { going: "✅ Dabei", maybe: "🤔 Vielleicht", declined: "❌ Absage", none: "– keine Reaktion" };

function attendanceName(row) {
  const wrapper = document.createElement("details");
  const summary = document.createElement("summary");
  summary.append(personCell(row.user));
  if (row.flagged) {
    const reasons = [];
    if (row.rate !== null && row.rate < state.attendance.rules.threshold) reasons.push(`Quote unter ${state.attendance.rules.threshold} %`);
    if (row.none >= state.attendance.rules.missing_limit) reasons.push(`${row.none}× keine Reaktion`);
    summary.append(" ", pill(`auffällig: ${reasons.join(", ")}`, "bad"));
  }
  wrapper.append(summary);
  const list = document.createElement("ul");
  for (const item of row.history) list.append(listItem(`${eventWhen(item.starts_at)} · ${item.title}: `, CHOICE_LABELS[item.choice]));
  if (!row.history.length) list.append(listItem(span("muted", "Keine Termine im Zeitraum (oder erst danach beigetreten).")));
  wrapper.append(list);
  return wrapper;
}

function renderAttendance() {
  const data = state.attendance;
  const { key, ascending } = state.attendanceSort;
  const value = (row) => (key === "name" ? row.user.name.toLowerCase() : row[key] ?? -1);
  const rows = [...data.players].sort((a, b) => {
    const order = value(a) < value(b) ? -1 : value(a) > value(b) ? 1 : 0;
    return ascending ? order : -order;
  });
  $("#attendance-list").replaceChildren(...(rows.length ? rows.map((row) => {
    const tableRowElement = tableRow(
      attendanceName(row), String(row.going), String(row.maybe), String(row.declined), String(row.none), String(row.total),
      row.rate === null ? "–" : `${row.rate} %`,
    );
    if (row.flagged) tableRowElement.classList.add("flagged");
    for (const cell of [...tableRowElement.children].slice(1)) cell.classList.add("num");
    return tableRowElement;
  }) : [emptyRow(7, "Keine Spieler mit der Rolle „Spieler“.")]));
  for (const header of document.querySelectorAll("#page-anwesenheit th")) {
    const button = header.querySelector("button.sort");
    if (button && button.dataset.sort === key) header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    else header.removeAttribute("aria-sort");
  }
  const flagged = data.players.filter((row) => row.flagged).length;
  $("#attendance-summary").textContent = `${data.events} Termine · ${data.players.length} Spieler · ${flagged} auffällig`;
}

async function loadAttendance() {
  const days = $("#attendance-days").value;
  $("#attendance-csv").href = `/api/attendance.csv?days=${days}`;
  state.attendance = await api(`/api/attendance?days=${days}`);
  const rules = state.attendance.rules;
  $("#attendance-rules").textContent = `Gezählt werden stattgefundene, nicht abgesagte Termine ab dem Beitritt. Markiert wird bei einer Quote unter ${rules.threshold} % oder ab ${rules.missing_limit} Terminen ohne Reaktion. Termine werden nach ${rules.retention_days} Tagen gelöscht, weiter reicht die Übersicht nicht zurück.`;
  renderAttendance();
}

function sortAttendance(key) {
  const current = state.attendanceSort;
  state.attendanceSort = { key, ascending: current.key === key ? !current.ascending : key === "name" };
  if (state.attendance) renderAttendance();
}

/* Rangliste */

const SVG_NS = "http://www.w3.org/2000/svg";

function rankScore(value) {
  return value ? value[0] * 4 + value[1] : 0;
}

function rankSparkline(history) {
  const points = history.filter((entry) => entry.rank_value);
  if (points.length < 2) return points.length ? span("muted small", "seit dem Verknüpfen unverändert") : "–";
  const width = 140;
  const height = 32;
  const scores = points.map((entry) => rankScore(entry.rank_value));
  const low = Math.min(...scores);
  const span_ = Math.max(...scores) - low || 1;
  const coordinates = points.map((entry, index) => [
    4 + (index * (width - 8)) / (points.length - 1),
    height - 4 - ((rankScore(entry.rank_value) - low) * (height - 8)) / span_,
  ]);
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Verlauf: ${points.map((entry) => `${entry.rank_tier} (${formatTime(entry.recorded_at)})`).join(", ")}`);
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", coordinates.map(([x, y]) => `${x},${y}`).join(" "));
  svg.append(line);
  coordinates.forEach(([x, y], index) => {
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    dot.setAttribute("r", 2.5);
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${points[index].rank_tier} · ${formatTime(points[index].recorded_at)}`;
    dot.append(title);
    svg.append(dot);
  });
  return svg;
}

async function loadRanking() {
  const { players } = await api("/api/ranking");
  const rows = players.map((player) => {
    const name = span("person", "");
    name.append(personCell(player.user));
    if (player.name !== player.user.name) name.append(span("muted small", `(Kader: ${player.name})`));
    return tableRow(
      `${player.position}.`, name, player.rank_tier || "noch kein Abgleich", formatTime(player.rank_synced_at),
      rankSparkline(player.history),
    );
  });
  $("#ranking-list").replaceChildren(...(rows.length ? rows : [emptyRow(5, "Noch kein Spieler hat seine Riot-ID verknüpft.")]));
}

/* Statistiken */

const decimal = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });

function rateCell(rate) {
  if (rate === null || rate === undefined) return "–";
  const wrapper = span("rate", "");
  const bar = span(`rate-bar${rate < 50 ? " low" : ""}`, "");
  bar.setAttribute("aria-hidden", "true");
  const fill = document.createElement("span");
  fill.style.width = `${Math.max(0, Math.min(100, rate))}%`;
  bar.append(fill);
  wrapper.append(bar, `${decimal.format(rate)} %`);
  return wrapper;
}

function statValue(value, suffix = "") {
  return value === null || value === undefined ? "–" : `${decimal.format(value)}${suffix}`;
}

function renderStats() {
  const data = state.stats;
  const { key, ascending } = state.statsSort;
  const value = (row) => (key === "name" ? row.user.name.toLowerCase() : row.summary[key] ?? -1);
  const rows = [...data.players].sort((a, b) => {
    const order = value(a) < value(b) ? -1 : value(a) > value(b) ? 1 : 0;
    return ascending ? order : -order;
  });
  $("#stats-list").replaceChildren(...(rows.length ? rows.map((row) => {
    const { summary } = row;
    const name = span("stack", "");
    name.append(personCell(row.user), span("muted small mono", row.riot_id));
    const details = document.createElement("button");
    details.type = "button";
    details.className = "btn btn-sm";
    details.textContent = "Details";
    details.setAttribute("aria-label", `Statistiken von ${row.user.name} anzeigen`);
    details.addEventListener("click", () => showPlayerStats(row).catch((error) => setBanner(error.message)));
    const element = tableRow(
      name, String(summary.matches), statValue(summary.kd), statValue(summary.hs_percent, " %"),
      statValue(summary.acs), statValue(summary.adr), rateCell(summary.win_rate),
      summary.agents.length ? summary.agents[0].name : "–", details,
    );
    for (const cell of [...element.children].slice(1, 6)) cell.classList.add("num");
    if (row.user.id === state.statsUser) element.classList.add("selected");
    return element;
  }) : [emptyRow(9, "Niemand hat eine Riot-ID verknüpft (/riot-id).")]));
  for (const header of document.querySelectorAll("#page-statistiken th")) {
    const button = header.querySelector("button.sort");
    if (button && button.dataset.sort === key) header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    else header.removeAttribute("aria-sort");
  }
  const withMatches = data.players.filter((row) => row.summary.matches).length;
  $("#stats-summary").textContent = `${data.players.length} Spieler · ${withMatches} mit Matches`;
}

async function loadStats() {
  const filter = $("#stats-players-only").checked ? "players" : "all";
  state.stats = await api(`/api/stats?filter=${filter}`);
  const rules = state.stats.rules;
  $("#stats-rules").textContent = rules.enabled
    ? `Ausgewertet werden die letzten ${rules.match_count} ${rules.only_competitive ? "Competitive-Matches" : "Matches (ohne Deathmatch)"} je Spieler aus der HenrikDev-API. `
      + `Der Bot holt sie täglich beim Rang-Abgleich und löscht Matches nach ${rules.retention_days} Tagen. ACS und ADR sind Punkte bzw. Schaden pro Runde.`
    : "Die Statistiken sind ausgeschaltet, weil der HenrikDev-API-Schlüssel fehlt (HENRIKDEV_API_KEY).";
  renderStats();
}

function sortStats(key) {
  const current = state.statsSort;
  state.statsSort = { key, ascending: current.key === key ? !current.ascending : key === "name" };
  if (state.stats) renderStats();
}

function statTile(label, value, hint = "") {
  const tile = span("stat-tile", "");
  tile.append(span("label", label), span("value", value), span("hint", hint));
  return tile;
}

const MATCH_RESULTS = { 1: ["Sieg", "ok"], 0: ["Niederlage", "bad"] };

function renderStatsDetail(data, title, meta, userId = null) {
  const { summary } = data;
  state.statsUser = userId;
  $("#stats-detail").hidden = false;
  $("#stats-detail-title").textContent = title;
  $("#stats-detail-meta").textContent = meta;
  $("#stats-refresh").hidden = !userId || state.me.level < 2;
  $("#stats-refresh-message").textContent = "";
  $("#stats-tiles").replaceChildren(
    statTile("K/D", statValue(summary.kd), `${summary.kills} / ${summary.deaths} / ${summary.assists}`),
    statTile("Headshot", statValue(summary.hs_percent, " %"), "aller Treffer"),
    statTile("ACS", statValue(summary.acs), "Punkte pro Runde"),
    statTile("ADR", statValue(summary.adr), "Schaden pro Runde"),
    statTile("Win-Rate", statValue(summary.win_rate, " %"), `${summary.wins} Siege · ${summary.losses} Niederlagen`),
    statTile("Matches", String(summary.matches), summary.matches ? "ausgewertet" : "noch keine"),
  );
  $("#stats-agents").replaceChildren(...(summary.agents.length
    ? summary.agents.map((agent) => tableRow(agent.name, String(agent.games), rateCell(agent.win_rate), statValue(agent.kd)))
    : [emptyRow(4, "Keine Matches.")]));
  $("#stats-maps").replaceChildren(...(summary.maps.length
    ? summary.maps.map((map) => tableRow(map.name, String(map.games), rateCell(map.win_rate)))
    : [emptyRow(3, "Keine Matches.")]));
  $("#stats-matches").replaceChildren(...(data.matches.length ? data.matches.map((match) => {
    const [label, kind] = MATCH_RESULTS[match.won] || ["Unentschieden", "neutral"];
    return tableRow(
      formatTime(match.started_at), match.map || "–", match.agent || "–", `${match.kills} / ${match.deaths} / ${match.assists}`,
      statValue(match.hs_percent, " %"), statValue(match.acs), pill(label, kind),
    );
  }) : [emptyRow(7, "Noch keine Matches gespeichert.")]));
  if (state.stats) renderStats();
  $("#stats-detail").scrollIntoView({ block: "start" });
}

async function showPlayerStats(row) {
  const data = await api(`/api/stats/${row.user.id}`);
  const meta = data.fetched_at
    ? `Zuletzt abgeholt: ${formatTime(data.fetched_at)}${data.rank_tier ? ` · Rang: ${data.rank_tier}` : ""}`
    : "Noch keine Matches abgeholt; der Bot holt sie beim nächsten täglichen Abgleich.";
  renderStatsDetail(data, `${data.user.name} · ${data.riot_id}`, meta, data.user.id);
}

async function refreshStats() {
  const userId = state.statsUser;
  const button = $("#stats-refresh");
  button.disabled = true;
  $("#stats-refresh-message").textContent = "Hole Matches …";
  try {
    const result = await api(`/api/stats/${userId}/refresh`, { method: "POST" });
    await loadStats();
    const row = state.stats.players.find((player) => player.user.id === userId);
    if (row) await showPlayerStats(row);
    $("#stats-refresh-message").textContent = result.message;
  } catch (error) {
    $("#stats-refresh-message").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function lookupStats(event) {
  event.preventDefault();
  const input = $("#stats-lookup-input");
  const button = event.submitter || $("#stats-lookup-form button[type=submit]");
  button.disabled = true;
  $("#stats-lookup-message").textContent = "Frage die HenrikDev-API ab …";
  try {
    const data = await api("/api/stats/lookup", { method: "POST", body: { riot_id: input.value } });
    $("#stats-lookup-message").textContent = "";
    renderStatsDetail(data, `Bewerber · ${data.riot_id}`, `Region ${data.region.toUpperCase()} · nicht gespeichert`);
  } catch (error) {
    $("#stats-lookup-message").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* Vorschau im Discord-Stil (vereinfacht, ohne verschachtelte Formatierung) */

const MARKDOWN_TOKENS = /(\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;
const MARKDOWN_TAGS = [["**", "strong"], ["__", "u"], ["~~", "s"], ["`", "code"], ["*", "em"], ["_", "em"]];

function inlineMarkdown(line) {
  const parts = [];
  for (const piece of line.split(MARKDOWN_TOKENS)) {
    if (!piece) continue;
    const tag = MARKDOWN_TAGS.find(([marker]) => piece.length > 2 * marker.length && piece.startsWith(marker) && piece.endsWith(marker));
    if (tag) {
      const element = document.createElement(tag[1]);
      element.textContent = piece.slice(tag[0].length, -tag[0].length);
      parts.push(element);
    } else {
      parts.push(document.createTextNode(piece));
    }
  }
  return parts;
}

function discordMarkdown(text) {
  const fragment = document.createDocumentFragment();
  text.split("\n").forEach((line, index) => {
    if (index) fragment.append(document.createTextNode("\n"));
    const heading = line.match(/^(#{1,3}) (.*)$/);
    if (heading) {
      const element = span(`md-h${heading[1].length}`, "");
      element.append(...inlineMarkdown(heading[2]));
      fragment.append(element);
    } else if (line.startsWith("> ")) {
      const quote = document.createElement("blockquote");
      quote.append(...inlineMarkdown(line.slice(2)));
      fragment.append(quote);
    } else {
      fragment.append(...inlineMarkdown(line));
    }
  });
  return fragment;
}

/* Ankündigungen */

function fieldRow(field = { name: "", value: "", inline: false }) {
  const row = document.createElement("div");
  row.className = "field-row";
  const name = document.createElement("input");
  name.className = "input input-sm";
  name.placeholder = "Feldname";
  name.maxLength = 256;
  name.value = field.name;
  name.setAttribute("aria-label", "Feldname");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn btn-sm";
  remove.textContent = "Entfernen";
  remove.addEventListener("click", () => { row.remove(); updateAnnouncementPreview(); });
  const value = document.createElement("textarea");
  value.className = "textarea textarea-sm";
  value.rows = 2;
  value.maxLength = 1024;
  value.placeholder = "Inhalt";
  value.value = field.value;
  value.setAttribute("aria-label", "Feldinhalt");
  const inline = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "checkbox checkbox-sm";
  box.checked = field.inline;
  inline.append(box, " nebeneinander");
  for (const input of [name, value, box]) input.addEventListener("input", updateAnnouncementPreview);
  row.append(name, remove, value, inline);
  return row;
}

function announcementPayload() {
  return {
    title: $("#announcement-title").value,
    description: $("#announcement-description").value,
    color: $("#announcement-color").value,
    image_url: $("#announcement-image").value.trim(),
    thumbnail_url: $("#announcement-thumbnail").value.trim(),
    footer: $("#announcement-footer").value,
    ping_role: $("#announcement-ping").value || null,
    fields: [...document.querySelectorAll("#announcement-fields .field-row")].map((row) => ({
      name: row.querySelector("input").value,
      value: row.querySelector("textarea").value,
      inline: row.querySelector("input[type=checkbox]").checked,
    })),
  };
}

function previewImage(url, className) {
  if (!/^https:\/\//.test(url)) return null;
  const image = document.createElement("img");
  image.src = url;
  image.alt = "";
  if (className) image.className = className;
  return image;
}

function updateAnnouncementPreview() {
  const payload = announcementPayload();
  const content = $("#announcement-preview-content");
  content.replaceChildren(payload.ping_role ? span("mention", `@${payload.ping_role}`) : "");
  const preview = $("#announcement-preview");
  preview.style.borderLeftColor = payload.color;
  const body = document.createElement("div");
  if (payload.title) body.append(span("embed-title", payload.title));
  if (payload.title) body.append(document.createElement("br"));
  if (payload.description) body.append(discordMarkdown(payload.description));
  const filledFields = payload.fields.filter((field) => field.name || field.value);
  if (filledFields.length) {
    const fields = span("embed-fields", "");
    for (const field of filledFields) {
      const item = span(`embed-field${field.inline ? " inline" : ""}`, "");
      item.append(span("embed-field-name", field.name), document.createElement("br"), discordMarkdown(field.value));
      fields.append(item);
    }
    body.append(fields);
  }
  const image = previewImage(payload.image_url);
  if (image) body.append(image);
  if (payload.footer) body.append(span("embed-footer", payload.footer));
  const thumbnail = previewImage(payload.thumbnail_url, "embed-thumbnail");
  preview.classList.toggle("with-thumbnail", Boolean(thumbnail));
  preview.replaceChildren(body, ...(thumbnail ? [thumbnail] : []));
}

function announcementWhen() {
  return document.querySelector("input[name=announcement-when]:checked").value;
}

function updateAnnouncementSubmit() {
  const later = announcementWhen() === "later";
  $("#announcement-send-at").disabled = !later;
  const sent = state.editingAnnouncement && state.editingAnnouncement.status === "sent";
  $("#announcement-submit").textContent = sent ? "Änderung speichern" : later ? "Planen" : "Senden …";
}

function fillAnnouncementForm(entry = null) {
  state.editingAnnouncement = entry;
  const payload = entry ? entry.payload : { title: "", description: "", color: "#eb1b29", image_url: "", thumbnail_url: "", footer: "", fields: [], ping_role: null };
  const sent = entry && entry.status === "sent";
  $("#announcement-form-title").textContent = entry ? (sent ? "Gesendete Ankündigung bearbeiten" : "Geplante Ankündigung bearbeiten") : "Neue Ankündigung";
  $("#announcement-sent-note").hidden = !sent;
  $("#announcement-title").value = payload.title;
  $("#announcement-description").value = payload.description;
  $("#announcement-color").value = payload.color;
  $("#announcement-image").value = payload.image_url || "";
  $("#announcement-thumbnail").value = payload.thumbnail_url || "";
  $("#announcement-footer").value = payload.footer;
  $("#announcement-ping").value = payload.ping_role || "";
  $("#announcement-fields").replaceChildren(...payload.fields.map(fieldRow));
  if (entry) $("#announcement-channel").value = entry.channel_id;
  for (const element of [$("#announcement-channel"), $("#announcement-ping")]) element.disabled = Boolean(sent);
  $("#announcement-when").disabled = Boolean(sent);
  const scheduled = entry && entry.status !== "sent" && entry.send_at;
  document.querySelector(`input[name=announcement-when][value=${scheduled ? "later" : "now"}]`).checked = true;
  $("#announcement-send-at").value = scheduled ? berlinInputValue(entry.send_at) : "";
  $("#announcement-send-at").min = berlinInputValue(Date.now() / 1000);
  $("#announcement-error").textContent = "";
  updateAnnouncementSubmit();
  updateAnnouncementPreview();
}

function announcementActions(entry) {
  const actions = span("row-actions", "");
  if (entry.url) actions.append(discordLink(entry.url, "Discord"));
  const editButton = document.createElement("button");
  editButton.type = "button";
  editButton.className = "btn btn-sm";
  editButton.textContent = "Bearbeiten";
  editButton.addEventListener("click", () => {
    fillAnnouncementForm(entry);
    $("#announcement-title").focus();
  });
  actions.append(editButton);
  if (entry.status !== "sent") {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-sm btn-error btn-outline";
    remove.textContent = "Löschen";
    remove.addEventListener("click", () => deleteAnnouncement(entry));
    actions.append(remove);
  }
  return actions;
}

const ANNOUNCEMENT_STATUS = { sent: ["Gesendet", "ok"], scheduled: ["Geplant", "warn"], failed: ["Fehlgeschlagen", "bad"] };

async function loadAnnouncements() {
  const data = await api("/api/announcements");
  state.announcementData = data;
  const selectedChannel = $("#announcement-channel").value;
  $("#announcement-channel").replaceChildren(...data.channels.map((channel) => {
    const option = document.createElement("option");
    option.value = channel.id;
    option.textContent = `#${channel.name}${channel.category ? ` (${channel.category})` : ""}`;
    return option;
  }));
  if (selectedChannel) $("#announcement-channel").value = selectedChannel;
  const selectedPing = $("#announcement-ping").value;
  $("#announcement-ping").replaceChildren(new Option("Niemand", ""), ...data.ping_roles.map((role) => new Option(`@${role}`, role)));
  $("#announcement-ping").value = selectedPing;
  const rows = data.announcements.map((entry) => {
    const [label, kind] = ANNOUNCEMENT_STATUS[entry.status];
    const status = span("", "");
    status.append(pill(label, kind));
    if (entry.error) status.append(" ", span("small form-error", entry.error));
    return tableRow(
      status, `#${entry.channel_name}`, entry.payload.title || entry.payload.description.slice(0, 60) || "–",
      formatTime(entry.sent_at || entry.send_at), personCell(entry.created_by), announcementActions(entry),
    );
  });
  $("#announcement-list").replaceChildren(...(rows.length ? rows : [emptyRow(6, "Noch keine Ankündigungen.")]));
  updateAnnouncementPreview();
}

async function submitAnnouncement(event) {
  event.preventDefault();
  const editing = state.editingAnnouncement;
  const later = announcementWhen() === "later";
  const body = {
    ...announcementPayload(),
    channel_id: $("#announcement-channel").value,
    send_at: later ? $("#announcement-send-at").value : null,
  };
  if (later && !body.send_at) {
    $("#announcement-error").textContent = "Bitte Datum und Uhrzeit für den geplanten Versand wählen.";
    return;
  }
  if ((!editing || editing.status !== "sent") && !later) {
    const channel = $("#announcement-channel").selectedOptions[0];
    const confirmed = await confirmAction(
      `Die Ankündigung wird sofort in ${channel ? channel.textContent : "dem Kanal"} veröffentlicht${body.ping_role ? ` und erwähnt @${body.ping_role}` : ", ohne jemanden zu erwähnen"}.`,
      null,
      "Jetzt senden",
    );
    if (!confirmed) return;
  }
  $("#announcement-submit").disabled = true;
  $("#announcement-error").textContent = "";
  try {
    const path = editing ? `/api/announcements/${editing.id}` : "/api/announcements";
    const result = await api(path, { method: "POST", body });
    fillAnnouncementForm();
    $("#announcement-message").textContent = result.message;
    await loadAnnouncements();
  } catch (error) {
    $("#announcement-error").textContent = error.message;
  } finally {
    $("#announcement-submit").disabled = false;
  }
}

async function deleteAnnouncement(entry) {
  const confirmed = await confirmAction(
    `Die geplante Ankündigung „${entry.payload.title || "ohne Titel"}“ wird gelöscht und nicht mehr gesendet.`, null, "Löschen",
  );
  if (!confirmed) return;
  try {
    const result = await api(`/api/announcements/${entry.id}`, { method: "DELETE" });
    if (state.editingAnnouncement && state.editingAnnouncement.id === entry.id) fillAnnouncementForm();
    $("#announcement-message").textContent = result.message;
    await loadAnnouncements();
  } catch (error) {
    $("#announcement-message").textContent = error.message;
  }
}

/* Server-Setup */

const SETUP_GROUPS = [
  ["delete", "Löschen", "bad"],
  ["create", "Neu", "ok"],
  ["change", "Ändern", "warn"],
  ["info", "Hinweise", "neutral"],
];
const SETUP_KINDS = {
  role: "Rolle", category: "Kategorie", channel: "Kanal", permissions: "Rechte", message: "Nachricht", member: "Mitglieder", data: "Daten",
};

function renderSetup(result, heading = null) {
  const groups = [];
  if (heading) {
    const title = document.createElement("h2");
    title.textContent = heading;
    groups.push(title);
  }
  for (const [action, label, kind] of SETUP_GROUPS) {
    const changes = result.changes.filter((change) => change.action === action);
    if (!changes.length) continue;
    const card = document.createElement("article");
    card.className = `card setup-group setup-${action}`;
    const title = document.createElement("h2");
    title.append(pill(String(changes.length), kind), label);
    const list = document.createElement("ul");
    for (const change of changes) list.append(listItem(span("kind", SETUP_KINDS[change.kind] || change.kind), change.text));
    card.append(title, list);
    groups.push(card);
  }
  if (!result.changes.length) groups.push(span("muted", "Nichts zu tun: Der Server entspricht der Einrichtung."));
  $("#setup-groups").replaceChildren(...groups);
  const { create, change, delete: deletions } = result.counts;
  $("#setup-summary").textContent = `${create} neu · ${change} ändern · ${deletions} löschen`;
}

async function loadSetup() {
  $("#setup-error").textContent = "";
  $("#setup-summary").textContent = "Vorschau wird berechnet …";
  $("#setup-apply").disabled = true;
  try {
    state.setupPreview = await api("/api/setup/preview");
    renderSetup(state.setupPreview);
    $("#setup-apply").disabled = !state.setupPreview.changes.some((change) => change.action !== "info");
  } catch (error) {
    $("#setup-summary").textContent = "";
    $("#setup-error").textContent = error.message;
  }
}

async function applySetup() {
  const { create, change, delete: deletions } = state.setupPreview.counts;
  const warning = deletions
    ? `Achtung: ${deletions} ${deletions === 1 ? "Löschung" : "Löschungen"}. Gelöschte Kanäle samt Nachrichten lassen sich nicht wiederherstellen.`
    : "Es wird nichts gelöscht.";
  const confirmed = await confirmAction(
    `Das Setup wird jetzt am echten Server angewendet: ${create} neu, ${change} geändert, ${deletions} gelöscht. ${warning}`,
    null,
    deletions ? `Anwenden und ${deletions} löschen` : "Anwenden",
  );
  if (!confirmed) return;
  $("#setup-apply").disabled = true;
  $("#setup-summary").textContent = "Wird angewendet …";
  try {
    const result = await api("/api/setup/apply", { method: "POST", body: { confirm: true, deletions } });
    renderSetup(result, "Ergebnis");
    state.setupPreview = null;
  } catch (error) {
    $("#setup-error").textContent = error.message;
    $("#setup-summary").textContent = "";
  }
}

/* Datenschutz */

const PRIVACY_LABELS = {
  profil: "Profil (Riot-ID, Rollen, Rang)",
  teamname: "Teamname",
  bewerbungen: "Bewerbungen",
  terminantworten: "Terminantworten",
  staff_stimmen: "Staff-Stimmen",
  tickets: "Tickets",
  eigene_voice_channels: "Eigene Voice-Kanäle",
  rangverlauf: "Rangverlauf",
  match_statistiken: "Match-Statistiken",
  geaenderte_einstellungen: "Geänderte Einstellungen (Panel)",
  geaenderte_texte: "Geänderte Texte (Panel)",
  ankuendigungen: "Ankündigungen (Panel)",
};

async function searchPrivacy(event) {
  event.preventDefault();
  const query = $("#privacy-query").value.trim();
  $("#privacy-search-message").textContent = "Suche …";
  try {
    const { results } = await api(`/api/privacy/search?q=${encodeURIComponent(query)}`);
    $("#privacy-results").replaceChildren(...(results.length ? results.map((result) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm";
      button.textContent = "Auskunft anzeigen";
      button.addEventListener("click", () => showPrivacy(result.user));
      return listItem(personCell(result.user), span("muted small", `${result.stored} gespeicherte Einträge`), button);
    }) : [listItem(span("muted", "Niemand gefunden. Für Ausgetretene bitte die User-ID eingeben."))]));
    $("#privacy-results").hidden = false;
    $("#privacy-search-message").textContent = "";
  } catch (error) {
    $("#privacy-search-message").textContent = error.message;
  }
}

async function showPrivacy(user) {
  state.privacyUser = user;
  const exportData = await api(`/api/privacy/${user.id}`);
  $("#privacy-person").textContent = `${user.name} · ${user.id}`;
  $("#privacy-sections").replaceChildren(...Object.entries(exportData.daten).map(([key, rows]) => tableRow(PRIVACY_LABELS[key] || key, String(rows.length))));
  $("#privacy-json").textContent = JSON.stringify(exportData, null, 2);
  $("#privacy-download").href = `/api/privacy/${user.id}?download=1`;
  $("#privacy-download").download = `duality-daten-${user.id}.json`;
  $("#privacy-reason").value = "";
  $("#privacy-error").textContent = "";
  $("#privacy-message").textContent = "";
  $("#privacy-detail").hidden = false;
  $("#privacy-person").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function deletePrivacy(event) {
  event.preventDefault();
  const user = state.privacyUser;
  const reason = $("#privacy-reason").value.trim();
  if (reason.length < 3) {
    $("#privacy-error").textContent = "Bitte „Grund / Anfrage vom“ angeben.";
    return;
  }
  const confirmed = await confirmAction(
    `Alle gespeicherten Daten von ${user.name} (${user.id}) werden endgültig gelöscht, inklusive Bewerbungsnachrichten im Staff-Kanal. Das lässt sich nicht rückgängig machen.`,
    `Grund / Anfrage vom: ${reason}`,
    "Endgültig löschen",
  );
  if (!confirmed) return;
  try {
    const result = await api(`/api/privacy/${user.id}/delete`, { method: "POST", body: { confirm: true, reason } });
    // Nicht neu laden: Das wäre eine weitere Auskunft im Audit-Log.
    $("#privacy-sections").replaceChildren(tableRow("Noch gespeichert (Nachweise)", String(result.remaining)));
    $("#privacy-json").textContent = "";
    $("#privacy-reason").value = "";
    $("#privacy-message").textContent = result.message;
  } catch (error) {
    $("#privacy-error").textContent = error.message;
  }
}

/* Texte */

function currentText() {
  return state.texts.find((entry) => entry.key === $("#text-select").value);
}

function renderedText(entry, value) {
  return Object.entries(entry.placeholders).reduce((result, [name, current]) => result.split(`{${name}}`).join(current), value);
}

function updateTextPreview() {
  const entry = currentText();
  if (!entry) return;
  const value = $("#text-editor").value;
  const rendered = renderedText(entry, value);
  const length = Math.max(value.trim().length, rendered.trim().length);
  const counter = $("#text-counter");
  counter.textContent = `${length} / ${entry.limit} Zeichen`;
  counter.classList.toggle("over-limit", length > entry.limit);
  $("#text-save").disabled = length > entry.limit || !value.trim();
  $("#text-preview").replaceChildren(discordMarkdown(rendered));
}

function showText() {
  const entry = currentText();
  if (!entry) return;
  $("#text-editor").value = entry.value;
  $("#text-error").textContent = "";
  $("#text-message").textContent = "";
  const placeholders = Object.entries(entry.placeholders).map(([name, value]) => `{${name}} = ${value}`);
  $("#text-placeholders").textContent = placeholders.length ? `Platzhalter: ${placeholders.join(", ")}` : "";
  $("#text-meta").textContent = entry.is_default
    ? "Standardtext"
    : `Geändert von ${entry.updated_by ? entry.updated_by.name : "unbekannt"} am ${formatTime(entry.updated_at)}`;
  $("#text-reset").disabled = entry.is_default;
  updateTextPreview();
  loadTextHistory(entry).catch((error) => setBanner(error.message));
}

async function loadTextHistory(entry) {
  const { history } = await api(`/api/texts/${entry.key}/history`);
  $("#text-history").replaceChildren(...(history.length ? history.map((version) => {
    const load = document.createElement("button");
    load.type = "button";
    load.className = "btn btn-sm";
    load.textContent = "In den Editor laden";
    load.addEventListener("click", () => {
      $("#text-editor").value = version.is_default ? entry.default : version.value;
      updateTextPreview();
      $("#text-editor").focus();
    });
    const preview = version.is_default ? "Standardtext" : `${version.value.slice(0, 80)}${version.value.length > 80 ? " …" : ""}`;
    return listItem(span("muted small", `${formatTime(version.updated_at)} · ${version.updated_by ? version.updated_by.name : "unbekannt"}`), preview, load);
  }) : [listItem(span("muted", "Noch keine Änderungen, es gilt der Standardtext."))]));
}

async function loadTexts() {
  const selected = $("#text-select").value;
  state.texts = (await api("/api/texts")).texts;
  $("#text-select").replaceChildren(...state.texts.map((entry) => {
    const option = document.createElement("option");
    option.value = entry.key;
    option.textContent = entry.label + (entry.is_default ? "" : " (geändert)");
    return option;
  }));
  if (selected && state.texts.some((entry) => entry.key === selected)) $("#text-select").value = selected;
  showText();
}

async function saveText(event) {
  event.preventDefault();
  const entry = currentText();
  $("#text-save").disabled = true;
  $("#text-error").textContent = "";
  try {
    const result = await api(`/api/texts/${entry.key}`, { method: "POST", body: { value: $("#text-editor").value } });
    await loadTexts();
    $("#text-message").textContent = result.message;
  } catch (error) {
    $("#text-error").textContent = error.message;
    updateTextPreview();
  }
}

async function resetText() {
  const entry = currentText();
  const confirmed = await confirmAction(
    `„${entry.label}“ wird auf den Standardtext aus dem Code zurückgesetzt. Die bisherige Fassung bleibt im Verlauf.`,
    null,
    "Zurücksetzen",
  );
  if (!confirmed) return;
  try {
    const result = await api(`/api/texts/${entry.key}/reset`, { method: "POST" });
    await loadTexts();
    $("#text-message").textContent = result.message;
  } catch (error) {
    $("#text-error").textContent = error.message;
  }
}

/* Einstellungen */

function settingInput(entry, id) {
  const input = document.createElement("input");
  input.id = id;
  input.className = entry.kind === "bool" ? "toggle toggle-primary" : "input input-sm";
  if (entry.kind === "bool") {
    input.type = "checkbox";
    input.checked = entry.value;
  } else if (entry.kind === "int") {
    input.type = "number";
    input.min = entry.minimum;
    input.max = entry.maximum;
    input.step = 1;
    input.required = true;
    input.value = entry.value;
  } else {
    input.type = "text";
    input.value = entry.value.join(", ");
    input.setAttribute("aria-describedby", `${id}-hint`);
  }
  return input;
}

function settingValue(entry, input) {
  if (entry.kind === "bool") return input.checked;
  if (entry.kind === "int") return Number(input.value);
  return input.value.split(/[,\s]+/).filter(Boolean).map(Number);
}

function settingText(kind, value) {
  if (kind === "bool") return value ? "an" : "aus";
  if (kind === "hours") return `${value.join(", ")} h`;
  return String(value);
}

function renderSetting(entry) {
  const card = document.createElement("article");
  card.className = "card setting";
  const id = `setting-${entry.key}`;
  const title = document.createElement("h2");
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = entry.label;
  title.append(label);
  const description = document.createElement("p");
  description.className = "muted small";
  description.textContent = entry.description;
  const form = document.createElement("form");
  const input = settingInput(entry, id);
  const save = document.createElement("button");
  save.type = "submit";
  save.className = "btn btn-sm btn-primary";
  save.textContent = "Speichern";
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "btn btn-sm";
  reset.textContent = "Standard";
  reset.disabled = entry.is_default;
  const message = span("small", "");
  message.setAttribute("role", "status");
  form.append(input);
  if (entry.kind === "hours") form.append(span("muted small", "Stunden, durch Komma getrennt"));
  form.append(save, reset, message);
  const meta = span("muted small", entry.is_default
    ? `Standard: ${settingText(entry.kind, entry.default)}`
    : `Standard: ${settingText(entry.kind, entry.default)} · geändert von ${entry.updated_by ? entry.updated_by.name : "unbekannt"} am ${formatTime(entry.updated_at)}`);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await changeSetting(`/api/settings/${entry.key}`, { value: settingValue(entry, input) }, message);
  });
  reset.addEventListener("click", () => changeSetting(`/api/settings/${entry.key}/reset`, undefined, message));
  card.append(title, description, form, meta);
  return card;
}

async function changeSetting(path, body, message) {
  message.className = "small";
  message.textContent = "Speichere …";
  try {
    await api(path, { method: "POST", body });
    await loadSettings();
  } catch (error) {
    message.className = "small form-error";
    message.textContent = error.message;
  }
}

async function loadSettings() {
  const { settings } = await api("/api/settings");
  $("#settings-list").replaceChildren(...settings.map(renderSetting));
}

async function loadTickets() {
  const filter = $("#ticket-filter").value;
  const { tickets } = await api(`/api/tickets?state=${filter}`);
  const rows = tickets.map((ticket) => tableRow(
    personCell(ticket.reporter),
    ticket.closed_at ? pill("Geschlossen", "neutral") : pill("Offen", "warn"),
    formatTime(ticket.created_at),
    formatTime(ticket.closed_at),
    discordLink(ticket.url, "Thread öffnen"),
  ));
  $("#ticket-list").replaceChildren(...(rows.length ? rows : [emptyRow(5, "Keine Tickets.")]));
  $("#ticket-count").textContent = `${rows.length} ${rows.length === 1 ? "Ticket" : "Tickets"}`;
}

const pageLoaders = {
  backups: loadBackups, bewerbungen: loadApplications, spieler: loadPlayers, tickets: loadTickets, termine: loadEvents,
  anwesenheit: loadAttendance, rangliste: loadRanking, statistiken: loadStats, einstellungen: loadSettings, texte: loadTexts,
  ankuendigungen: loadAnnouncements, setup: loadSetup,
};

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
    title.textContent = channel.name;
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

function confirmAction(text, detail, okLabel = "Ausführen", optionText = null) {
  const dialog = $("#confirm-dialog");
  $("#confirm-text").textContent = text;
  $("#confirm-command").textContent = detail || "";
  $("#confirm-command").hidden = !detail;
  $("#confirm-ok").textContent = okLabel;
  // Optionale Zusatzfrage; den Haken liest der Aufrufer danach aus #confirm-option.
  $("#confirm-option-row").hidden = !optionText;
  $("#confirm-option-text").textContent = optionText || "";
  $("#confirm-option").checked = false;
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

// Überzeile über jedem Seitentitel aus der Navigationsgruppe, z. B. „Duality Mannheim // Team“.
function addEyebrows() {
  for (const group of document.querySelectorAll("#nav .nav-group")) {
    const title = group.querySelector(".menu-title").textContent;
    for (const link of group.querySelectorAll("a[data-page]")) {
      const heading = document.querySelector(`#page-${link.dataset.page} > h1`);
      if (!heading) continue;
      const eyebrow = document.createElement("p");
      eyebrow.className = "eyebrow";
      eyebrow.textContent = `Duality Mannheim // ${title}`;
      heading.before(eyebrow);
    }
  }
}

async function init() {
  addEyebrows();
  state.me = await api("/api/me");
  $("#user-name").textContent = state.me.name;
  $("#user-role").textContent = state.me.role;
  $("#user-avatar").src = state.me.avatar;
  state.historyIndex = state.history.length;
  // Menüpunkte ohne Berechtigung ausblenden; der Server prüft die Rechte trotzdem bei jeder Anfrage.
  for (const element of document.querySelectorAll("[data-level]")) {
    const target = element.closest("#nav") ? element.closest("li") : element;
    target.hidden = Number(element.dataset.level) > state.me.level;
  }
  for (const group of document.querySelectorAll("#nav .nav-group")) {
    group.hidden = [...group.querySelectorAll("li")].every((item) => item.hidden);
  }
  $("#logout").addEventListener("click", logout);
  $("#command-form").addEventListener("submit", submitCommand);
  $("#command-input").addEventListener("keydown", handleCommandKeys);
  $("#log-pause").addEventListener("click", togglePause);
  $("#backup-create").addEventListener("click", createBackup);
  $("#voice-hide-empty").addEventListener("change", () => renderVoice(state.voice));
  $("#application-filter").addEventListener("change", () => loadApplications().catch((error) => setBanner(error.message)));
  $("#new-season").addEventListener("click", startNewSeason);
  $("#players-only").addEventListener("change", () => loadPlayers().catch((error) => setBanner(error.message)));
  $("#player-search").addEventListener("input", renderPlayers);
  $("#ticket-filter").addEventListener("change", () => loadTickets().catch((error) => setBanner(error.message)));
  $("#events-range").addEventListener("change", () => loadEvents().catch((error) => setBanner(error.message)));
  $("#attendance-days").addEventListener("change", () => loadAttendance().catch((error) => setBanner(error.message)));
  for (const button of document.querySelectorAll("#page-anwesenheit button.sort")) {
    button.addEventListener("click", () => sortAttendance(button.dataset.sort));
  }
  $("#stats-players-only").addEventListener("change", () => loadStats().catch((error) => setBanner(error.message)));
  for (const button of document.querySelectorAll("#page-statistiken button.sort")) {
    button.addEventListener("click", () => sortStats(button.dataset.sort));
  }
  $("#stats-lookup-form").addEventListener("submit", lookupStats);
  $("#stats-refresh").addEventListener("click", refreshStats);
  $("#setup-refresh").addEventListener("click", loadSetup);
  $("#setup-apply").addEventListener("click", applySetup);
  $("#privacy-search-form").addEventListener("submit", searchPrivacy);
  $("#privacy-delete-form").addEventListener("submit", deletePrivacy);
  $("#announcement-form").addEventListener("submit", submitAnnouncement);
  $("#announcement-form").addEventListener("input", updateAnnouncementPreview);
  $("#announcement-form").addEventListener("change", () => { updateAnnouncementSubmit(); updateAnnouncementPreview(); });
  $("#announcement-add-field").addEventListener("click", () => {
    if (document.querySelectorAll("#announcement-fields .field-row").length >= 10) return;
    $("#announcement-fields").append(fieldRow());
    updateAnnouncementPreview();
  });
  $("#announcement-clear").addEventListener("click", () => fillAnnouncementForm());
  $("#text-select").addEventListener("change", showText);
  $("#text-editor").addEventListener("input", updateTextPreview);
  $("#text-form").addEventListener("submit", saveText);
  $("#text-reset").addEventListener("click", resetText);
  $("#events-view-list").addEventListener("click", () => showEventsView("list"));
  $("#events-view-calendar").addEventListener("click", () => showEventsView("calendar"));
  $("#calendar-prev").addEventListener("click", () => moveCalendar(-1));
  $("#calendar-next").addEventListener("click", () => moveCalendar(1));
  $("#event-new").addEventListener("click", () => openEventDialog());
  $("#event-form").addEventListener("submit", saveEvent);
  $("#event-cancel-dialog").addEventListener("click", () => $("#event-dialog").close());
  $("#log-search").addEventListener("input", renderLog);
  for (const box of document.querySelectorAll("input[name=log-level]")) box.addEventListener("change", renderLog);
  window.addEventListener("hashchange", showPage);
  showPage();
  setInterval(renderUptime, 1000);
  connect();
}

init().catch((error) => setBanner(`Das Panel konnte nicht geladen werden: ${error.message}`));
