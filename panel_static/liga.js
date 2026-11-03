"use strict";

// Öffentliche Liga-Seite: Der Schlüssel steckt in der Adresse (/liga/<schlüssel>), die Daten kommen von …/data.
// Alles wird per textContent gesetzt; Namen aus der API landen nie als HTML in der Seite.

const state = { data: null, sort: { key: "acs", ascending: false }, round: null };

const $ = (selector) => document.querySelector(selector);
const decimal = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });
const dateTime = new Intl.DateTimeFormat("de-DE", {
  timeZone: "Europe/Berlin", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
});
const BADGES = { ok: "badge-success", bad: "badge-error", neutral: "badge-quiet" };
const RESULTS = { 1: ["Sieg", "ok"], 0: ["Niederlage", "bad"] };
const ROUND_ENDS = {
  elimination: ["✕", "Eliminierung"], detonate: ["✹", "Spike explodiert"], defuse: ["✓", "Spike entschärft"],
  time: ["◷", "Zeit abgelaufen"], surrender: ["⚑", "Aufgabe"],
};
const BUYS = { pistol: "Pistol", eco: "Eco", half: "Halb", full: "Voll" };
const SIDES = { attack: "Angriff", defense: "Verteidigung" };
const CEREMONIES = { Ace: "Ace", TeamAce: "Team-Ace", Clutch: "Clutch", Flawless: "Flawless", Thrifty: "Thrifty", Closer: "Closer" };

/* Bausteine */

function element(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

function span(className, text) {
  return element("span", className, text);
}

function pill(text, kind) {
  return span(`badge badge-sm ${BADGES[kind] || BADGES.neutral}`, text);
}

function link(href, text) {
  const anchor = element("a", "", text);
  anchor.href = href;
  return anchor;
}

function tableRow(cells, numeric = []) {
  const row = document.createElement("tr");
  cells.forEach((value, index) => {
    const cell = document.createElement("td");
    cell.append(value ?? "–");
    if (numeric.includes(index)) cell.classList.add("num");
    row.append(cell);
  });
  return row;
}

function emptyRow(columns, text) {
  const row = document.createElement("tr");
  const cell = element("td", "empty", text);
  cell.colSpan = columns;
  row.append(cell);
  return row;
}

function statTile(label, value, hint = "") {
  const tile = span("stat-tile", "");
  tile.append(span("label", label), span("value", value), span("hint", hint));
  return tile;
}

function value(number, suffix = "") {
  return number === null || number === undefined ? "–" : `${decimal.format(number)}${suffix}`;
}

function signed(number) {
  if (number === null || number === undefined) return "–";
  return number > 0 ? `+${number}` : String(number);
}

function rate(record) {
  return value(record.rate, " %");
}

function formatTime(seconds) {
  return `${dateTime.format(new Date(seconds * 1000))} Uhr`;
}

function clock(milliseconds) {
  const seconds = Math.floor((milliseconds || 0) / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function riotId(player) {
  return player.tag ? `${player.name}#${player.tag}` : player.name;
}

function result(match) {
  const [label, kind] = RESULTS[match.won] || ["Unentschieden", "neutral"];
  const wrapper = span("row-actions", "");
  wrapper.append(pill(label, kind), `${match.score.us} : ${match.score.them}`);
  return wrapper;
}

function playerLink(player) {
  const wrapper = span("stack", "");
  wrapper.append(link(`#spieler/${encodeURIComponent(player.puuid)}`, riotId(player)));
  return wrapper;
}

function multiKills(multi) {
  const parts = Object.entries(multi).filter(([, count]) => count).map(([key, count]) => `${count}× ${key.toUpperCase()}`);
  return parts.length ? parts.join(" · ") : "–";
}

/* Übersicht */

function renderOverview() {
  const { summary, matches } = state.data;
  $("#overview-tiles").replaceChildren(
    statTile("Bilanz", `${summary.wins}–${summary.losses}`, `${summary.matches} ${summary.matches === 1 ? "Match" : "Matches"}${summary.draws ? ` · ${summary.draws} unentschieden` : ""}`),
    statTile("Runden", signed(summary.rounds_won - summary.rounds_lost), `${summary.rounds_won} : ${summary.rounds_lost}`),
    statTile("Angriff", rate(summary.attack), `${summary.attack.won} von ${summary.attack.played} Runden`),
    statTile("Verteidigung", rate(summary.defense), `${summary.defense.won} von ${summary.defense.played} Runden`),
    statTile("Pistol", rate(summary.pistol), `${summary.pistol.won} von ${summary.pistol.played} Runden`),
    statTile("Eco gewonnen", String(summary.eco.won), `von ${summary.eco.played} Eco-Runden`),
  );
  $("#overview-matches").replaceChildren(...(matches.length ? matches.map((match) => {
    const opponent = span("stack", "");
    opponent.append(link(`#match/${match.match_id}`, match.opponent));
    if (match.label) opponent.append(span("muted small", match.label));
    const ours = match.players.filter((player) => player.team === "us");
    const best = ours.reduce((top, player) => (!top || (player.acs || 0) > (top.acs || 0) ? player : top), null);
    const bestCell = best ? span("stack", "") : "–";
    if (best) bestCell.append(link(`#spieler/${encodeURIComponent(best.puuid)}`, riotId(best)), span("muted small", `${best.agent} · ${best.acs} ACS`));
    return tableRow([formatTime(match.started_at), opponent, match.map || "–", result(match), bestCell]);
  }) : [emptyRow(5, "Noch keine Matches ausgewählt.")]));
  $("#overview-maps").replaceChildren(...(summary.maps.length ? summary.maps.map((map) => tableRow(
    [map.name, String(map.games), `${map.wins}–${map.losses}`, signed(map.round_diff), value(map.win_rate, " %")], [1, 2, 3],
  )) : [emptyRow(5, "Noch keine Maps.")]));
  $("#overview-top").replaceChildren(...(summary.players.length ? summary.players.slice(0, 5).map((player) => tableRow(
    [playerLink(player), value(player.acs), value(player.kd), value(player.kast, " %")], [1, 2, 3],
  )) : [emptyRow(4, "Noch keine Spieler.")]));
}

/* Spieler-Tabelle */

function renderPlayers() {
  const { key, ascending } = state.sort;
  const sortValue = (player) => (key === "name" ? player.name.toLowerCase() : player[key] ?? -1);
  const players = [...state.data.summary.players].sort((a, b) => {
    const order = sortValue(a) < sortValue(b) ? -1 : sortValue(a) > sortValue(b) ? 1 : 0;
    return ascending ? order : -order;
  });
  $("#players-list").replaceChildren(...(players.length ? players.map((player) => tableRow([
    playerLink(player), String(player.matches), player.agents.map((agent) => agent.name).join(", "),
    value(player.acs), value(player.kd), `${player.kills} / ${player.deaths} / ${player.assists}`,
    value(player.adr), value(player.hs_percent, " %"), value(player.kast, " %"),
    String(player.fk), String(player.fd), String(player.clutches),
  ], [1, 3, 4, 5, 6, 7, 8, 9, 10, 11])) : [emptyRow(12, "Noch keine Spieler.")]));
  for (const header of document.querySelectorAll("#players-table th")) {
    const button = header.querySelector("button.sort");
    if (button && button.dataset.sort === key) header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    else header.removeAttribute("aria-sort");
  }
}

function sortPlayers(key) {
  const current = state.sort;
  state.sort = { key, ascending: current.key === key ? !current.ascending : key === "name" };
  renderPlayers();
}

/* Match */

const BOARD_COLUMNS = [
  ["Spieler", null], ["Agent", null], ["ACS", "acs"], ["K", "kills"], ["D", "deaths"], ["A", "assists"], ["+/–", null],
  ["ADR", "adr"], ["HS %", "hs_percent"], ["KAST", "kast"], ["FK", "fk"], ["FD", "fd"], ["Multi-Kills", null], ["Clutches", "clutches"],
];

function boardHead() {
  const row = document.createElement("tr");
  BOARD_COLUMNS.forEach(([label], index) => {
    const header = element("th", index >= 2 && index !== 12 ? "num" : "", label);
    header.scope = "col";
    row.append(header);
  });
  return row;
}

function boardRows(players, own) {
  return players.map((player) => tableRow([
    own ? playerLink(player) : riotId(player), player.agent, value(player.acs), String(player.kills), String(player.deaths),
    String(player.assists), signed(player.kills - player.deaths), value(player.adr), value(player.hs_percent, " %"),
    value(player.kast, " %"), String(player.fk), String(player.fd), multiKills(player.multi), String(player.clutches),
  ], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13]));
}

function roundChip(round, match) {
  const [symbol, endLabel] = ROUND_ENDS[round.end] || ROUND_ENDS.time;
  const chip = element("button", `round-chip ${round.winner === "us" ? "win" : round.winner === "them" ? "loss" : ""} ${round.side}`);
  chip.type = "button";
  chip.append(span("round-number", String(round.number)), span("round-symbol", symbol));
  const outcome = round.winner === "us" ? "gewonnen" : round.winner === "them" ? "verloren" : "ohne Sieger";
  chip.setAttribute("aria-label", `Runde ${round.number}: ${outcome}, ${SIDES[round.side]}, ${endLabel}`);
  chip.setAttribute("aria-pressed", String(state.round === round.number));
  chip.addEventListener("click", () => {
    state.round = state.round === round.number ? null : round.number;
    renderRounds(match);
  });
  return chip;
}

function renderRounds(match) {
  const strip = $("#match-rounds");
  strip.replaceChildren();
  match.rounds.forEach((round) => {
    // Lücke zur Halbzeit und vor der Verlängerung
    if (round.number === 13 || round.number === 25) strip.append(span("round-gap", ""));
    strip.append(roundChip(round, match));
  });
  renderRoundDetail(match);
}

function playerName(match, index) {
  if (index === null || index === undefined) return span("muted", "–");
  const player = match.players[index];
  return span(player.team === "us" ? "team-us" : "team-them", `${riotId(player)} (${player.agent})`);
}

function renderRoundDetail(match) {
  const detail = $("#round-detail");
  const round = match.rounds.find((entry) => entry.number === state.round);
  detail.hidden = !round;
  if (!round) return;
  const [, endLabel] = ROUND_ENDS[round.end] || ROUND_ENDS.time;
  const heading = element("h3", "", `Runde ${round.number}: ${round.winner === "us" ? "gewonnen" : round.winner === "them" ? "verloren" : "ohne Sieger"}`);
  const facts = element("dl", "stat-list round-facts");
  const add = (label, content) => {
    const description = element("dd");
    description.append(content);
    facts.append(element("dt", "", label), description);
  };
  add("Seite", SIDES[round.side]);
  add("Ende", endLabel + (round.ceremony ? ` · ${CEREMONIES[round.ceremony] || round.ceremony}` : ""));
  add("Ausrüstung", `${BUYS[round.buy.us]} (${round.loadout.us.toLocaleString("de-DE")}) gegen ${BUYS[round.buy.them]} (${round.loadout.them.toLocaleString("de-DE")})`);
  if (round.plant) {
    const plant = span("", "");
    plant.append(playerName(match, round.plant.player), ` · ${round.plant.site ? `Site ${round.plant.site} · ` : ""}${clock(round.plant.time)}`);
    add("Spike gelegt", plant);
  }
  if (round.defuse) {
    const defuse = span("", "");
    defuse.append(playerName(match, round.defuse.player), ` · ${clock(round.defuse.time)}`);
    add("Entschärft", defuse);
  }
  if (round.clutch) {
    const clutch = span("", "");
    clutch.append(playerName(match, round.clutch.player), ` · 1v${round.clutch.enemies}`);
    add("Clutch", clutch);
  }
  const feed = element("ol", "kill-feed");
  for (const kill of round.kills) {
    const item = element("li");
    item.append(span("muted", clock(kill.time)), " ", playerName(match, kill.killer), " → ", playerName(match, kill.victim));
    if (kill.weapon) item.append(span("muted", ` · ${kill.weapon}`));
    if (kill.assists.length) item.append(span("muted", ` · Assists: ${kill.assists.map((index) => riotId(match.players[index])).join(", ")}`));
    feed.append(item);
  }
  if (!round.kills.length) feed.append(element("li", "muted", "Keine Kills."));
  detail.replaceChildren(heading, facts, element("h3", "small muted", "Kills"), feed);
}

function renderMatch(match) {
  const [label] = RESULTS[match.won] || ["Unentschieden"];
  $("#match-eyebrow").textContent = `Duality Mannheim // ${match.label || "OPLeague"}`;
  $("#title-match").textContent = `${label} ${match.score.us} : ${match.score.them} gegen ${match.opponent}`;
  document.title = `${match.opponent} · Liga-Stats · Duality`;
  const minutes = Math.round(match.duration / 60);
  $("#match-meta").textContent = [
    match.map || "Unbekannte Map", formatTime(match.started_at), minutes ? `${minutes} min` : null,
    `Halbzeit ${match.halftime.us} : ${match.halftime.them}`,
  ].filter(Boolean).join(" · ");
  const us = match.teams.us;
  const ours = match.players.filter((player) => player.team === "us");
  const best = ours.reduce((top, player) => (!top || (player.acs || 0) > (top.acs || 0) ? player : top), null);
  $("#match-tiles").replaceChildren(
    statTile("Angriff", `${us.attack.won}/${us.attack.played}`, "Runden gewonnen"),
    statTile("Verteidigung", `${us.defense.won}/${us.defense.played}`, "Runden gewonnen"),
    statTile("Pistol", `${us.pistol.won}/${us.pistol.played}`, "Runden gewonnen"),
    statTile("First Kills", `${us.fk}–${match.teams.them.fk}`, "eigene – Gegner"),
    statTile("Bester Spieler", best ? String(best.acs) : "–", best ? `${riotId(best)} · ACS` : ""),
  );
  state.round = null;
  renderRounds(match);
  $("#caption-us").textContent = "Duality";
  $("#caption-them").textContent = match.opponent;
  $("#head-us").replaceChildren(boardHead());
  $("#head-them").replaceChildren(boardHead());
  $("#board-us").replaceChildren(...boardRows(ours, true));
  $("#board-them").replaceChildren(...boardRows(match.players.filter((player) => player.team === "them"), false));
  $("#compare-them").textContent = match.opponent;
  const them = match.teams.them;
  const record = (entry) => `${entry.won}/${entry.played}`;
  $("#match-compare").replaceChildren(
    ...[
      ["Angriffsrunden", record(us.attack), record(them.attack)],
      ["Verteidigungsrunden", record(us.defense), record(them.defense)],
      ["Pistol-Runden", record(us.pistol), record(them.pistol)],
      ["Eco-Runden gewonnen", record(us.eco), record(them.eco)],
      ["Full-Buy-Runden gewonnen", record(us.full_buy), record(them.full_buy)],
      ["First Kills", String(us.fk), String(them.fk)],
      ["Spike gelegt", String(us.plants), String(them.plants)],
      ["Spike entschärft", String(us.defuses), String(them.defuses)],
      ["Clutches", String(us.clutches), String(them.clutches)],
    ].map((cells) => tableRow(cells, [1, 2])),
  );
}

/* Spieler */

function renderPlayer(puuid) {
  const player = state.data.summary.players.find((entry) => entry.puuid === puuid);
  if (!player) return false;
  $("#title-player").textContent = riotId(player);
  document.title = `${riotId(player)} · Liga-Stats · Duality`;
  $("#player-tiles").replaceChildren(
    statTile("Matches", String(player.matches), `${player.wins} gewonnen`),
    statTile("ACS", value(player.acs), "Punkte pro Runde"),
    statTile("K/D", value(player.kd), `${player.kills} / ${player.deaths} / ${player.assists}`),
    statTile("ADR", value(player.adr), `Δ ${signed(player.dd_delta)} pro Runde`),
    statTile("HS %", value(player.hs_percent, " %"), "aller Treffer"),
    statTile("KAST", value(player.kast, " %"), `${player.kast_rounds} von ${player.rounds} Runden`),
    statTile("FK / FD", `${player.fk} / ${player.fd}`, "erste Kills / Tode"),
    statTile("Clutches", String(player.clutches), `${player.plants} Plants · ${player.defuses} Defuses`),
  );
  $("#player-agents").replaceChildren(...player.agents.map((agent) => tableRow([agent.name, String(agent.games)], [1])));
  $("#player-multi").replaceChildren(...Object.entries(player.multi).map(([key, count]) => tableRow([key.toUpperCase(), String(count)], [1])));
  const rows = [];
  for (const match of state.data.matches) {
    const entry = match.players.find((candidate) => candidate.puuid === puuid);
    if (!entry) continue;
    rows.push(tableRow([
      formatTime(match.started_at), link(`#match/${match.match_id}`, match.opponent), match.map || "–", entry.agent,
      `${entry.kills} / ${entry.deaths} / ${entry.assists}`, value(entry.acs), value(entry.adr), value(entry.hs_percent, " %"),
      value(entry.kast, " %"), result(match),
    ], [4, 5, 6, 7, 8]));
  }
  $("#player-matches").replaceChildren(...rows);
  return true;
}

/* Navigation */

function show(view) {
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.id !== `view-${view}`;
  for (const anchor of document.querySelectorAll(".liga-nav a")) {
    const active = anchor.dataset.view === view;
    anchor.classList.toggle("btn-primary", active);
    if (active) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }
}

function route() {
  if (!state.data) return;
  const [view, id] = window.location.hash.slice(1).split("/");
  const key = id ? decodeURIComponent(id) : null;
  document.title = "Liga-Stats · Duality";
  $("#liga-status").hidden = true;
  if (view === "match" && key) {
    const match = state.data.matches.find((entry) => entry.match_id === key);
    if (match) {
      renderMatch(match);
      show("match");
      window.scrollTo(0, 0);
      return;
    }
  } else if (view === "spieler" && key) {
    if (renderPlayer(key)) {
      show("player");
      window.scrollTo(0, 0);
      return;
    }
  } else if (view === "spieler") {
    renderPlayers();
    show("players");
    return;
  }
  renderOverview();
  show("overview");
}

async function load() {
  const status = $("#liga-status");
  try {
    const response = await fetch(`${window.location.pathname.replace(/\/$/, "")}/data`, { credentials: "omit", cache: "no-store" });
    if (response.status === 404) throw new Error("Dieser Link gilt nicht mehr. Den aktuellen Link findest du angepinnt in #liga-stats.");
    if (response.status === 429) throw new Error("Zu viele Anfragen. Bitte lade die Seite gleich neu.");
    if (!response.ok) throw new Error(`Die Liga-Stats konnten nicht geladen werden (Fehler ${response.status}).`);
    state.data = await response.json();
  } catch (error) {
    status.textContent = error.message;
    return;
  }
  for (const button of document.querySelectorAll("#players-table button.sort")) {
    button.addEventListener("click", () => sortPlayers(button.dataset.sort));
  }
  window.addEventListener("hashchange", route);
  route();
}

load();
