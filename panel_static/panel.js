"use strict";

const state = { me: null };

const $ = (selector) => document.querySelector(selector);

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
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } finally {
    window.location.href = "/";
  }
}

async function init() {
  state.me = await api("/api/me");
  $("#user-name").textContent = state.me.name;
  $("#user-role").textContent = state.me.role;
  $("#user-avatar").src = state.me.avatar;
  // Menüpunkte ohne Berechtigung ausblenden; der Server prüft die Rechte trotzdem bei jeder Anfrage.
  for (const link of document.querySelectorAll("#nav a")) {
    link.parentElement.hidden = Number(link.dataset.level) > state.me.level;
  }
  $("#logout").addEventListener("click", logout);
  window.addEventListener("hashchange", showPage);
  showPage();
}

init().catch((error) => {
  const banner = $("#connection-banner");
  banner.textContent = `Das Panel konnte nicht geladen werden: ${error.message}`;
  banner.hidden = false;
});
