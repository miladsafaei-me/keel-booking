/*
  Keel Booking widget: month calendar, time slots and the details form of one meeting type.

  It binds to data attributes only, never to class names, so the host owns every element and
  class. Markup it fills is cloned from the host's <template data-kb-template="..."> elements
  (weekday, day, blank, slot). The JSON config is the element #kb-config (Django json_script).
  State is shown with attributes the host styles: aria-pressed, aria-current="date", disabled,
  hidden, and the is-now / is-done classes on [data-kb-stepper-item].

  Language: dates and numbers follow the page's <html lang> (always on the Gregorian calendar, which
  the month grid is), the week starts on config.weekStart (0 is Sunday), and every sentence comes
  from config.labels, which the view fills in the page's language.
*/
(function () {
  "use strict";

  var app = document.querySelector("[data-kb-app]");
  var configNode = document.getElementById("kb-config");
  if (!app || !configNode) { return; }
  var config = JSON.parse(configNode.textContent);
  var labels = config.labels || {};

  function $(name) { return app.querySelector("[data-kb-" + name + "]"); }
  function template(name) {
    var node = app.querySelector('template[data-kb-template="' + name + '"]');
    return node.content.firstElementChild.cloneNode(true);
  }
  function text(key, fallback, values) {
    var value = labels[key] || fallback;
    Object.keys(values || {}).forEach(function (k) { value = value.replace("{" + k + "}", values[k]); });
    return value;
  }

  var pageLocale = document.documentElement.lang || navigator.language || "en";
  // The visitor's own regional settings (12 or 24 hours) when the browser speaks the page's language.
  var browser = navigator.language || "";
  var sameLanguage = browser.split("-")[0].toLowerCase() === pageLocale.split("-")[0].toLowerCase();
  var locale = gregorian(pageLocale);
  var timeLocale = gregorian(sameLanguage ? browser : pageLocale);
  var weekStart = typeof config.weekStart === "number" ? config.weekStart : 1;
  var numbers = new Intl.NumberFormat(locale, { useGrouping: false });

  function gregorian(tag) {
    try { return Intl.getCanonicalLocales(tag.indexOf("-u-") < 0 ? tag + "-u-ca-gregory" : tag)[0]; } catch (e) { return tag; }
  }
  var state = { tz: config.timezone, month: null, data: null, date: null, slot: null, busy: false };

  function guessTimezone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || config.timezone; } catch (e) { return config.timezone; }
  }

  function offsetLabel(tz) {
    try {
      var parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" }).formatToParts(new Date());
      var part = parts.filter(function (p) { return p.type === "timeZoneName"; })[0];
      return part ? part.value : "";
    } catch (e) { return ""; }
  }

  function zoneName(tz) { return tz.replace(/_/g, " "); }

  function fillTimezones() {
    var select = $("timezone");
    if (!select) { return; }
    var zones = [];
    try { zones = Intl.supportedValuesOf("timeZone"); } catch (e) { zones = []; }
    [config.timezone, state.tz].forEach(function (tz) { if (zones.indexOf(tz) < 0) { zones.unshift(tz); } });
    select.textContent = "";
    zones.forEach(function (tz) {
      var option = document.createElement("option");
      option.value = tz;
      option.textContent = zoneName(tz) + " (" + offsetLabel(tz) + ")";
      if (tz === state.tz) { option.selected = true; }
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      state.tz = select.value;
      state.date = null;
      state.slot = null;
      load(state.month);
    });
  }

  function parseMonth(value) {
    var bits = value.split("-");
    return { year: parseInt(bits[0], 10), month: parseInt(bits[1], 10) };
  }

  function shiftMonth(value, step) {
    var m = parseMonth(value);
    var d = new Date(Date.UTC(m.year, m.month - 1 + step, 1));
    return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0");
  }

  function monthTitle(value) {
    var m = parseMonth(value);
    return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" })
      .format(new Date(Date.UTC(m.year, m.month - 1, 1)));
  }

  function dayTitle(iso) {
    var bits = iso.split("-");
    return new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" })
      .format(new Date(Date.UTC(+bits[0], +bits[1] - 1, +bits[2])));
  }

  function timeOf(isoUtc) {
    return new Intl.DateTimeFormat(timeLocale, { hour: "numeric", minute: "2-digit", timeZone: state.tz }).format(new Date(isoUtc));
  }

  function endOf(isoUtc) {
    return new Date(new Date(isoUtc).getTime() + config.duration * 60000).toISOString();
  }

  function setLoading(on) {
    var node = $("loading");
    if (node) { node.hidden = !on; }
    app.setAttribute("aria-busy", on ? "true" : "false");
  }

  function renderWeekdays() {
    var row = $("weekdays");
    if (!row || row.childElementCount) { return; }
    // 7 January 2024 was a Sunday. Languages whose short names run long (Persian) take one letter, as their calendars do.
    var days = [], style = "short";
    for (var n = 0; n < 7; n++) { days.push(new Date(Date.UTC(2024, 0, 7 + weekStart + n))); }
    var short = new Intl.DateTimeFormat(locale, { weekday: "short", timeZone: "UTC" });
    if (days.some(function (day) { return short.format(day).length > 4; })) { style = "narrow"; }
    var format = new Intl.DateTimeFormat(locale, { weekday: style, timeZone: "UTC" });
    for (var i = 0; i < 7; i++) {
      var cell = template("weekday");
      cell.textContent = format.format(days[i]);
      row.appendChild(cell);
    }
  }

  function renderMonth() {
    var data = state.data;
    var grid = $("days");
    grid.textContent = "";
    $("month-label").textContent = monthTitle(data.month);
    $("prev").disabled = data.month <= data.firstMonth;
    $("next").disabled = data.month >= data.lastMonth;
    var m = parseMonth(data.month);
    var first = new Date(Date.UTC(m.year, m.month - 1, 1));
    var lead = (first.getUTCDay() - weekStart + 7) % 7;
    var count = new Date(Date.UTC(m.year, m.month, 0)).getUTCDate();
    for (var b = 0; b < lead; b++) { grid.appendChild(template("blank")); }
    var dayLabel = new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });
    for (var d = 1; d <= count; d++) {
      var iso = data.month + "-" + String(d).padStart(2, "0");
      var open = (data.days[iso] || []).length > 0;
      var button = template("day");
      button.textContent = numbers.format(d);
      button.setAttribute("data-date", iso);
      button.disabled = !open;
      button.setAttribute("aria-pressed", iso === state.date ? "true" : "false");
      var label = dayLabel.format(new Date(Date.UTC(m.year, m.month - 1, d)));
      button.setAttribute("aria-label", open ? text("dayOpen", "{day}, times available", { day: label }) : text("dayClosed", "{day}, no times", { day: label }));
      if (iso === data.today) { button.setAttribute("aria-current", "date"); }
      grid.appendChild(button);
    }
    var empty = $("month-empty");
    if (empty) { empty.hidden = Object.keys(data.days).length > 0; }
  }

  function renderSlots() {
    var list = $("slots");
    var label = $("date-label");
    var empty = $("empty");
    list.textContent = "";
    if (!state.date) {
      if (label) { label.textContent = text("pickDay", "Pick a day"); }
      if (empty) { empty.hidden = true; }
      return;
    }
    if (label) { label.textContent = dayTitle(state.date); }
    var slots = (state.data && state.data.days[state.date]) || [];
    if (empty) { empty.hidden = slots.length > 0; }
    slots.forEach(function (iso) {
      var item = template("slot");
      var button = item.matches("button") ? item : item.querySelector("button");
      button.textContent = timeOf(iso);
      button.setAttribute("data-start", iso);
      button.setAttribute("aria-pressed", iso === state.slot ? "true" : "false");
      list.appendChild(item);
    });
  }

  function load(month) {
    setLoading(true);
    var url = config.slotsUrl + "?tz=" + encodeURIComponent(state.tz) + (month ? "&month=" + month : "");
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (response) { if (!response.ok) { throw new Error(response.status); } return response.json(); })
      .then(function (data) {
        state.data = data;
        state.month = data.month;
        if (state.date && !(data.days[state.date] || []).length) { state.date = null; }
        if (!state.date) {
          var open = Object.keys(data.days).sort();
          if (open.length) { state.date = open[0]; }
        }
        renderMonth();
        renderSlots();
        var tzLabel = $("timezone-label");
        if (tzLabel) { tzLabel.textContent = zoneName(state.tz); }
        return data;
      })
      .catch(function () { showError(text("loadFailed", "The calendar did not load. Please refresh the page.")); })
      .then(function (data) { setLoading(false); return data; });
  }

  function step(name) {
    app.querySelectorAll("[data-kb-step]").forEach(function (node) { node.hidden = node.getAttribute("data-kb-step") !== name; });
    var order = ["pick", "details", "done"];
    var at = order.indexOf(name);
    app.querySelectorAll("[data-kb-stepper-item]").forEach(function (node) {
      var i = order.indexOf(node.getAttribute("data-kb-stepper-item"));
      node.classList.toggle("is-done", i < at);
      node.classList.toggle("is-now", i === at);
      if (i === at) { node.setAttribute("aria-current", "step"); } else { node.removeAttribute("aria-current"); }
    });
  }

  function showError(message) {
    var node = $("error");
    if (!node) { return; }
    node.hidden = !message;
    var body = node.querySelector("[data-kb-error-text]") || node;
    body.textContent = message || "";
  }

  function summary() {
    var node = $("summary");
    if (!node || !state.slot) { return; }
    node.textContent = text("summary", "{day}, {start} to {end}", { day: dayTitle(state.date), start: timeOf(state.slot), end: timeOf(endOf(state.slot)) });
    var tz = $("summary-timezone");
    if (tz) { tz.textContent = zoneName(state.tz) + " (" + offsetLabel(state.tz) + ")"; }
  }

  function bind() {
    $("prev").addEventListener("click", function () { state.date = null; load(shiftMonth(state.month, -1)); });
    $("next").addEventListener("click", function () { state.date = null; load(shiftMonth(state.month, 1)); });
    $("days").addEventListener("click", function (event) {
      var button = event.target.closest("[data-date]");
      if (!button || button.disabled) { return; }
      state.date = button.getAttribute("data-date");
      state.slot = null;
      $("days").querySelectorAll("[data-date]").forEach(function (b) { b.setAttribute("aria-pressed", b === button ? "true" : "false"); });
      renderSlots();
      var list = $("slots");
      if (list && window.matchMedia("(max-width: 61.99em)").matches) { list.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
    });
    $("slots").addEventListener("click", function (event) {
      var button = event.target.closest("[data-start]");
      if (!button) { return; }
      state.slot = button.getAttribute("data-start");
      summary();
      showError("");
      step("details");
      var first = $("form").querySelector("input:not([type=hidden]):not([tabindex='-1'])");
      if (first) { first.focus(); }
    });
    var back = $("back");
    if (back) { back.addEventListener("click", function () { step("pick"); renderSlots(); }); }

    $("form").addEventListener("submit", function (event) {
      event.preventDefault();
      if (state.busy) { return; }
      var form = event.target;
      app.querySelectorAll("[data-kb-field-error]").forEach(function (node) { node.hidden = true; });
      form.querySelectorAll("[aria-invalid]").forEach(function (node) { node.removeAttribute("aria-invalid"); });
      var payload = new FormData(form);
      payload.set("start", state.slot);
      payload.set("timezone", state.tz);
      var fileProblem = checkFiles(form);
      if (fileProblem) { fieldError("files", fileProblem); showError(text("fixFields", "Please check the highlighted fields.")); return; }
      var submit = $("submit");
      state.busy = true;
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      showError("");
      fetch(config.reserveUrl, {
        method: "POST",
        headers: { "X-CSRFToken": config.csrfToken || readCookie("csrftoken") },
        body: payload
      })
        .then(function (response) { return response.json().then(function (body) { return { status: response.status, body: body }; }); })
        .then(function (result) {
          if (result.status === 201 && result.body.redirect) {
            step("done");
            window.location.assign(result.body.redirect);
            return;
          }
          if (result.status === 422 && result.body.fields) {
            Object.keys(result.body.fields).forEach(function (field) { fieldError(field, result.body.fields[field]); });
            if (result.body.fields.start) { step("pick"); }
            showError(text("fixFields", "Please check the highlighted fields."));
            return;
          }
          if (result.status === 409) {
            state.slot = null;
            step("pick");
            load(state.month).then(function () { showError(result.body.message); });
            return;
          }
          showError(result.body.message || text("failed", "The booking did not go through. Please try again."));
        })
        .catch(function () { showError(text("failed", "The booking did not go through. Please try again.")); })
        .then(function () { state.busy = false; submit.disabled = false; submit.removeAttribute("aria-busy"); });
    });
  }

  function fieldError(field, message) {
    var form = $("form");
    var node = app.querySelector('[data-kb-field-error="' + field + '"]');
    var input = form.elements[field] || form.querySelector('[data-kb-field="' + field + '"]');
    if (node) { node.hidden = false; (node.querySelector("[data-kb-error-text]") || node).textContent = message; }
    if (input && input.setAttribute) { input.setAttribute("aria-invalid", "true"); }
  }

  function checkFiles(form) {
    var input = form.elements.files;
    if (!input || !input.files) { return ""; }
    var files = Array.prototype.slice.call(input.files);
    if (files.length > config.maxFiles) { return text("tooManyFiles", "You can attach up to {n} files.", { n: numbers.format(config.maxFiles) }); }
    for (var i = 0; i < files.length; i++) {
      var ext = files[i].name.indexOf(".") >= 0 ? files[i].name.split(".").pop().toLowerCase() : "";
      if (config.fileExtensions.indexOf(ext) < 0) { return text("badFileType", "{name}: this file type is not accepted.", { name: files[i].name }); }
      if (files[i].size > config.maxFileMb * 1048576) { return text("fileTooLarge", "{name} is larger than {mb} MB.", { name: files[i].name, mb: numbers.format(config.maxFileMb) }); }
    }
    return "";
  }

  function size(bytes) {
    var mb = new Intl.NumberFormat(locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 });
    return bytes >= 1048576 ? mb.format(bytes / 1048576) + " MB" : numbers.format(Math.max(1, Math.round(bytes / 1024))) + " KB";
  }

  function bindFiles() {
    var form = $("form");
    var input = form.elements.files;
    var list = $("files");
    if (!input || !list) { return; }
    input.addEventListener("change", function () {
      list.textContent = "";
      var node = app.querySelector('[data-kb-field-error="files"]');
      if (node) { node.hidden = true; }
      Array.prototype.forEach.call(input.files, function (file) {
        var item = template("file");
        var name = item.querySelector("[data-kb-file-name]") || item;
        name.textContent = file.name;
        var meta = item.querySelector("[data-kb-file-size]");
        if (meta) { meta.textContent = size(file.size); }
        list.appendChild(item);
      });
      var problem = checkFiles(form);
      if (problem) { fieldError("files", problem); }
    });
  }

  function bindSocials() {
    var list = $("socials");
    var add = $("social-add");
    if (!list || !add) { return; }
    function refresh() {
      var rows = list.querySelectorAll("[data-kb-social-row]");
      rows.forEach(function (row) { row.querySelector("[data-kb-social-remove]").hidden = rows.length === 1; });
      add.hidden = rows.length >= config.maxSocials;
    }
    add.addEventListener("click", function () {
      var rows = list.querySelectorAll("[data-kb-social-row]");
      var row = rows[rows.length - 1].cloneNode(true);
      row.querySelectorAll("input").forEach(function (input) { input.value = ""; });
      list.appendChild(row);
      refresh();
      row.querySelector("select, input").focus();
    });
    list.addEventListener("click", function (event) {
      var remove = event.target.closest("[data-kb-social-remove]");
      if (!remove) { return; }
      var row = remove.closest("[data-kb-social-row]");
      var other = row.previousElementSibling || row.nextElementSibling;
      row.remove();
      refresh();
      if (other) { other.querySelector("input").focus(); }
    });
    refresh();
  }

  function readCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  if (config.detectTimezone) { state.tz = guessTimezone(); }
  fillTimezones();
  renderWeekdays();
  bind();
  bindFiles();
  bindSocials();
  step("pick");
  load(null);
})();
