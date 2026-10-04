/* The restaurant's own screens.
 *
 * One script for both, because they are one job: open a restaurant, then look
 * after it. It is an ordinary client of the public API — the same fetch, the same
 * bearer token in the same browser storage the diner's script uses — so the
 * console cannot do anything the API would refuse, and every rule about who may
 * do what lives in the service rather than in this file.
 */
(function () {
  "use strict";

  const SESSION_KEY = "tablekeeper.session";

  function readSession() {
    try {
      const raw = window.localStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.token ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  async function api(path, options) {
    const settings = options || {};
    const headers = {"Accept": "application/json"};
    if (settings.body !== undefined) headers["Content-Type"] = "application/json";
    if (settings.key) headers["Idempotency-Key"] = settings.key;
    const session = readSession();
    if (session && session.token) headers.Authorization = `Bearer ${session.token}`;
    let response;
    try {
      response = await fetch(path, {
        method: settings.method || "GET",
        headers,
        body: settings.body === undefined ? undefined : JSON.stringify(settings.body),
      });
    } catch (networkError) {
      return {ok: false, status: 0, payload: null, lost: true};
    }
    let payload = null;
    try { payload = await response.json(); } catch (parseError) { payload = null; }
    return {ok: response.ok, status: response.status, payload, lost: false};
  }

  function newKey() {
    const bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  /* ---------------------------------------------------------------------- */
  /* small DOM helpers                                                      */
  /* ---------------------------------------------------------------------- */
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function say(node, message, kind) {
    if (!node) return;
    node.textContent = "";
    if (!message) return;
    const notice = element("p", `notice notice-${kind || "error"}`, message);
    node.appendChild(notice);
  }

  function signedOut(where) {
    say(where, "Please sign in first — then come back to this page.", "error");
  }

  function describeError(result, fallback) {
    if (result.lost) return "The service did not answer. Your change may or may not have been saved — try again.";
    if (result.payload && result.payload.error && result.payload.error.message) {
      return result.payload.error.message;
    }
    return fallback || `Something went wrong (${result.status}).`;
  }

  /* ---------------------------------------------------------------------- */
  /* /start — open a restaurant                                             */
  /* ---------------------------------------------------------------------- */
  function rowFrom(templateId) {
    const template = document.getElementById(templateId);
    const row = template.content.firstElementChild.cloneNode(true);
    row.querySelectorAll(".remove-row").forEach((button) => {
      button.addEventListener("click", () => row.remove());
    });
    return row;
  }

  function tableLabels() {
    return Array.from(document.querySelectorAll("#table-rows .table-row"))
      .map((row) => {
        const label = row.querySelector(".table-label").value.trim();
        const capacity = Number(row.querySelector(".table-capacity").value);
        return label ? {label, capacity} : null;
      })
      .filter(Boolean);
  }

  function refreshPairOptions() {
    const labels = tableLabels().map((table) => table.label);
    document.querySelectorAll("#pair-rows .pair-row").forEach((row) => {
      ["pair-first", "pair-second"].forEach((className) => {
        const select = row.querySelector(`.${className}`);
        const chosen = select.value;
        select.textContent = "";
        labels.forEach((label) => {
          const option = element("option", null, label);
          option.value = label;
          select.appendChild(option);
        });
        if (labels.includes(chosen)) select.value = chosen;
      });
    });
  }

  function setBusy(form, busy) {
    form.querySelectorAll("button, input, select").forEach((control) => {
      if (control.classList.contains("remove-row")) return;
      control.disabled = busy;
    });
  }

  function readStartForm() {
    const hours = Array.from(document.querySelectorAll("#hours-rows .hours-row"))
      .map((row) => ({
        weekday: row.querySelector(".hours-weekday").value,
        opens: row.querySelector(".hours-opens").value,
        closes: row.querySelector(".hours-closes").value,
      }));
    const tables = Array.from(document.querySelectorAll("#table-rows .table-row"))
      .map((row, index) => ({
        id: `t_${index + 1}`,
        label: row.querySelector(".table-label").value.trim(),
        capacity: Number(row.querySelector(".table-capacity").value),
      }));
    const byLabel = new Map(tables.map((table) => [table.label, table.id]));
    const combinable = Array.from(document.querySelectorAll("#pair-rows .pair-row"))
      .map((row) => {
        const first = byLabel.get(row.querySelector(".pair-first").value);
        const second = byLabel.get(row.querySelector(".pair-second").value);
        return first && second ? [first, second] : null;
      })
      .filter(Boolean);
    return {
      name: document.getElementById("start-name").value.trim(),
      timezone: document.getElementById("start-timezone").value.trim(),
      slot_minutes: Number(document.getElementById("start-slot").value),
      reservation_duration_minutes: Number(document.getElementById("start-duration").value),
      cancellation_cutoff_minutes: Number(document.getElementById("start-cutoff").value),
      opening_hours: hours,
      tables,
      combinable,
    };
  }

  function wireStart() {
    const form = document.getElementById("start-form");
    if (!form) return;
    const status = document.getElementById("start-status");
    const result = document.getElementById("start-result");

    document.getElementById("add-hours").addEventListener("click", () => {
      document.getElementById("hours-rows").appendChild(rowFrom("hours-row-template"));
    });
    document.getElementById("add-table").addEventListener("click", () => {
      document.getElementById("table-rows").appendChild(rowFrom("table-row-template"));
      refreshPairOptions();
    });
    document.getElementById("add-pair").addEventListener("click", () => {
      refreshPairOptions();
      document.getElementById("pair-rows").appendChild(rowFrom("pair-row-template"));
      refreshPairOptions();
    });
    document.getElementById("table-rows").addEventListener("input", refreshPairOptions);

    // A restaurant that cannot be described yet is worse than one that can:
    // start with one service and one table, and let the owner add more.
    document.getElementById("hours-rows").appendChild(rowFrom("hours-row-template"));
    document.getElementById("table-rows").appendChild(rowFrom("table-row-template"));
    document.getElementById("table-rows").appendChild(rowFrom("table-row-template"));
    const tables = document.querySelectorAll("#table-rows .table-row");
    tables[0].querySelector(".table-label").value = "1";
    tables[1].querySelector(".table-label").value = "2";
    tables[1].querySelector(".table-capacity").value = "4";

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!readSession()) return signedOut(status);
      const body = readStartForm();
      if (!body.name) return say(status, "Your restaurant needs a name.", "error");
      // One key per submission: a retry after a lost response must not open a
      // second restaurant.
      setBusy(form, true);
      say(status, "Opening your restaurant…", "success");
      const response = await api("/restaurants", {
        method: "POST", body, key: newKey(),
      });
      setBusy(form, false);
      if (!response.ok) return say(status, describeError(response), "error");
      say(status, "");
      result.textContent = "";
      const panel = element("div", "panel");
      panel.appendChild(element("h2", "label", "Your restaurant is open"));
      panel.appendChild(element("p", null,
        `${response.payload.name} is live. Its id is ${response.payload.id}.`));
      const link = element("a", "button button-primary", "Open your console");
      link.href = `/console?restaurant_id=${encodeURIComponent(response.payload.id)}`;
      panel.appendChild(link);
      result.appendChild(panel);
      form.reset();
    });
  }

  /* ---------------------------------------------------------------------- */
  /* /console — run a restaurant                                            */
  /* ---------------------------------------------------------------------- */
  function summaryPanel(title, rows) {
    const panel = element("section", "panel console-card");
    panel.appendChild(element("h2", "label", title));
    if (!rows.length) {
      panel.appendChild(element("p", "hint", "Nothing here yet."));
      return panel;
    }
    const list = element("ul", "console-list");
    rows.forEach((row) => {
      const item = element("li");
      if (typeof row === "string") { item.textContent = row; }
      else { item.appendChild(row); }
      list.appendChild(item);
    });
    panel.appendChild(list);
    return panel;
  }

  function hoursText(hours) {
    if (!hours || !hours.length) return "No service published";
    return hours.map((h) => `${h.weekday} ${h.opens}–${h.closes}`).join(", ");
  }

  async function drawConsole(restaurantId) {
    const body = document.getElementById("console-body");
    const status = document.getElementById("console-status");
    body.textContent = "";
    say(status, "Loading…", "success");

    const detail = await api(`/restaurants/${encodeURIComponent(restaurantId)}/staff`);
    if (detail.status === 401) return signedOut(status);
    if (detail.status === 404) {
      return say(status, "That restaurant is not yours to look after.", "error");
    }
    if (!detail.ok) return say(status, describeError(detail), "error");
    say(status, "");
    const restaurant = detail.payload;

    const head = element("section", "panel console-card");
    head.appendChild(element("h1", "title", restaurant.name));
    head.appendChild(element("p", "hint",
      `${restaurant.timezone} · ${restaurant.slot_minutes}-minute slots · ` +
      `${restaurant.reservation_duration_minutes}-minute sittings · ` +
      `${restaurant.cancellation_cutoff_minutes}-minute cancellation cutoff`));
    head.appendChild(element("p", "hint", hoursText(restaurant.opening_hours)));
    body.appendChild(head);

    const grid = element("div", "console-cards");
    body.appendChild(grid);

    grid.appendChild(summaryPanel(
      `Tables (${restaurant.tables.length})`,
      restaurant.tables.map((table) =>
        `${table.label} · seats ${table.capacity}`),
    ));
    grid.appendChild(summaryPanel(
      "Tables that join",
      (restaurant.combinable || []).map(([first, second]) => {
        const label = (id) => {
          const found = restaurant.tables.find((table) => table.id === id);
          return found ? found.label : id;
        };
        return `${label(first)} + ${label(second)}`;
      }),
    ));
    grid.appendChild(summaryPanel(
      `Staff (${restaurant.staff.length})`,
      restaurant.staff.map((person) =>
        `${person.display_name} · ${person.role}${person.email ? ` · ${person.email}` : ""}`),
    ));

    // The outbox: what the restaurant has told its diners, and what it has not.
    const outbox = await api(
      `/restaurants/${encodeURIComponent(restaurantId)}/notifications`);
    if (outbox.ok) {
      const counts = outbox.payload.summary;
      const panel = summaryPanel(
        "Messages to diners",
        outbox.payload.notifications.slice(0, 8).map((message) => {
          const line = element("span");
          line.textContent = `${message.subject} — ${message.to_email} `;
          const state = element("strong", null, message.status);
          line.appendChild(state);
          if (message.last_error) {
            line.appendChild(element("span", "hint", ` (${message.last_error})`));
          }
          return line;
        }),
      );
      panel.appendChild(element("p", "hint",
        `${counts.sent} sent · ${counts.queued} waiting · ${counts.failed} failed`));

      const deliver = element("button", "button button-quiet button-small",
        "Deliver what is waiting");
      deliver.type = "button";
      deliver.addEventListener("click", async () => {
        const result = await api(
          `/restaurants/${encodeURIComponent(restaurantId)}/notifications/drain`,
          {method: "POST"});
        if (!result.ok) return say(status, describeError(result), "error");
        if (!result.payload.configured) {
          return say(status,
            `${result.payload.queued} message(s) are waiting, but this service has ` +
            "no mail server configured, so nothing was sent.", "uncertain");
        }
        say(status, `Delivered ${result.payload.sent}, failed ${result.payload.failed}.`,
          result.payload.failed ? "uncertain" : "success");
        drawConsole(restaurantId);
      });
      panel.appendChild(deliver);
      grid.appendChild(panel);
    }

    // The record: who did what here.
    const audit = await api(`/restaurants/${encodeURIComponent(restaurantId)}/audit`);
    if (audit.ok) {
      grid.appendChild(summaryPanel(
        "Recent changes",
        audit.payload.entries.slice(0, 10).map((entry) =>
          `${entry.action.replace(/_/g, " ")} — ${entry.display_name || entry.user_id} ` +
          `at ${entry.created_at}`),
      ));
    }

    // Plans are proposed and applied by the API, and a manager's client reads
    // them from the same places it reads everything else.
    const plans = element("section", "panel console-card");
    plans.appendChild(element("h2", "label", "Planning around a closed table"));
    plans.appendChild(element("p", "hint",
      "Previewing a plan and applying it are API operations today: POST " +
      "/restaurants/{id}/replans, then POST /restaurants/{id}/replans/{plan_id}/apply. " +
      "A screen for them is the next thing this console gets."));
    body.appendChild(plans);
  }

  async function wireConsole() {
    const picker = document.getElementById("restaurant-picker");
    if (!picker) return;
    const status = document.getElementById("console-status");
    if (!readSession()) return signedOut(status);

    const mine = await api("/restaurants/mine");
    if (mine.status === 401) return signedOut(status);
    if (!mine.ok) return say(status, describeError(mine), "error");
    const restaurants = mine.payload.restaurants || [];
    restaurants.forEach((restaurant) => {
      const option = element("option", null, `${restaurant.name} (${restaurant.role})`);
      option.value = restaurant.id;
      picker.appendChild(option);
    });
    if (!restaurants.length) {
      say(status, "You do not work at a restaurant yet.", "error");
      return;
    }

    const wanted = new URLSearchParams(window.location.search).get("restaurant_id");
    const chosen = restaurants.some((r) => r.id === wanted) ? wanted : restaurants[0].id;
    picker.value = chosen;
    picker.addEventListener("change", () => drawConsole(picker.value));
    drawConsole(chosen);
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireStart();
    wireConsole();
  });
})();
