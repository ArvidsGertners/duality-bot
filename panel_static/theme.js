// Setzt das Farbschema, bevor die Seite gezeichnet wird; die Wahl bleibt nur in diesem Browser gespeichert.
(() => {
  const KEY = "duality-theme";
  const ORDER = ["system", "light", "dark"];
  const LABELS = { system: "System", light: "Hell", dark: "Dunkel" };
  const query = window.matchMedia("(prefers-color-scheme: light)");

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch {
      return "system";
    }
  }

  function apply(choice) {
    const light = choice === "light" || (choice === "system" && query.matches);
    document.documentElement.dataset.theme = light ? "duality-light" : "duality-dark";
  }

  apply(stored());
  query.addEventListener("change", () => apply(stored()));

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    let choice = stored();
    const label = () => {
      button.textContent = `Theme: ${LABELS[choice]}`;
      button.setAttribute("aria-label", `Darstellung: ${LABELS[choice]} (klicken zum Wechseln)`);
    };
    label();
    button.addEventListener("click", () => {
      choice = ORDER[(ORDER.indexOf(choice) + 1) % ORDER.length];
      try {
        if (choice === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, choice);
      } catch {
        // Ohne Speicher gilt die Wahl nur bis zum Neuladen.
      }
      apply(choice);
      label();
    });
  });
})();
