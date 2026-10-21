// Setzt das Farbschema, bevor die Seite gezeichnet wird; die Wahl bleibt nur in diesem Browser gespeichert.
(() => {
  const KEY = "duality-theme";
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
    const select = document.getElementById("theme-select");
    if (!select) return;
    select.value = stored();
    select.addEventListener("change", () => {
      try {
        if (select.value === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, select.value);
      } catch {
        // Ohne Speicher gilt die Wahl nur bis zum Neuladen.
      }
      apply(select.value);
    });
  });
})();
