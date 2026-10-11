// Stash: small helpers, no framework.
(function () {
  // Confirm before destructive forms (or a destructive button inside one), then show a busy label.
  document.addEventListener("submit", function (e) {
    var f = e.target, b = e.submitter;
    if (f.dataset && f.dataset.confirm && !window.confirm(f.dataset.confirm)) { e.preventDefault(); return; }
    if (b && b.dataset.confirmAction && !window.confirm(b.dataset.confirmAction)) { e.preventDefault(); return; }
    var busy = (b && b.dataset.busyLabel) || (f.dataset && f.dataset.busy);
    if (busy && b) setTimeout(function () { b.textContent = busy; b.disabled = true; }, 0);
  });

  // Tap a listing-draft box to copy it (eBay page).
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-copy]");
    if (!el) return;
    el.select();
    var done = function () {
      var tag = el.parentNode.querySelector(".copied") || document.createElement("span");
      tag.className = "copied small";
      tag.textContent = " Copied";
      el.parentNode.insertBefore(tag, el);
      setTimeout(function () { tag.remove(); }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(el.value).then(done, function () { document.execCommand("copy"); done(); });
    else { document.execCommand("copy"); done(); }
  });

  // Print buttons (the insurance report).
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-print]")) window.print();
  });

  // Snap to add: send the chosen photos to Claude and fill in the empty fields.
  var photos = document.getElementById("photos"), fill = document.getElementById("snapfill");
  if (photos && fill) {
    var status = document.getElementById("snapstatus"), form = document.getElementById("itemform");
    var setField = function (name, value) {
      var el = form.elements[name];
      if (!el || !value || el.value.trim()) return false;
      el.value = value;
      var d = el.closest("details");
      if (d) d.open = true;
      return true;
    };
    var run = function () {
      if (!photos.files.length) return;
      var fd = new FormData();
      Array.prototype.slice.call(photos.files, 0, 6).forEach(function (f) { fd.append("photos", f); });
      fill.disabled = true;
      status.textContent = "Claude is looking at the photo" + (photos.files.length > 1 ? "s" : "") + "...";
      fetch("/api/ai/identify", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) { status.textContent = d.error; return; }
          var got = [];
          [["name", "name"], ["manufacturer", "maker"], ["model", "model"], ["serial", "serial number"],
           ["description", "description"]].forEach(function (p) { if (setField(p[0], d[p[0]])) got.push(p[1]); });
          (d.labels || []).forEach(function (id) {
            var box = form.querySelector('input[name="labels"][value="' + id + '"]');
            if (box && !box.checked) { box.checked = true; got.push("a label"); }
          });
          status.textContent = got.length
            ? "Filled in " + got.join(", ") + ". Check them before saving." + (d.confidence === "low" ? " Claude was not sure about this one." : "")
            : "Nothing new to fill in: those fields already have values.";
        })
        .catch(function () { status.textContent = "Could not reach Stash. Try again."; })
        .then(function () { fill.disabled = !photos.files.length; });
    };
    photos.addEventListener("change", function () {
      fill.disabled = !photos.files.length;
      if (photos.files.length && !form.elements.name.value.trim()) run();  // new item: go straight away
    });
    fill.addEventListener("click", run);
  }

  // Bulk edit on the Items page.
  var grid = document.getElementById("itemgrid"), bulk = document.getElementById("bulk"),
      toggle = document.getElementById("selecttoggle");
  if (grid && bulk && toggle) {
    var boxes = function () { return grid.querySelectorAll(".pickbox"); };
    var count = function () {
      var n = grid.querySelectorAll(".pickbox:checked").length;
      document.getElementById("selcount").textContent = n;
      bulk.querySelectorAll("button[name=action]").forEach(function (b) { b.disabled = !n; });
    };
    var mode = function (on) {
      grid.classList.toggle("selecting", on);
      bulk.hidden = !on;
      toggle.hidden = on;
      if (!on) boxes().forEach(function (b) { b.checked = false; });
      count();
    };
    toggle.addEventListener("click", function () { mode(true); });
    document.getElementById("selectdone").addEventListener("click", function () { mode(false); });
    document.getElementById("selectall").addEventListener("click", function () {
      boxes().forEach(function (b) { b.checked = true; });
      count();
    });
    grid.addEventListener("click", function (e) {
      if (!grid.classList.contains("selecting")) return;
      var card = e.target.closest(".pickable");
      if (!card) return;
      if (e.target.classList.contains("pickbox")) { count(); return; }
      e.preventDefault();
      var b = card.querySelector(".pickbox");
      b.checked = !b.checked;
      count();
    });
  }

  // Filter controls apply on change (inline onchange is blocked by the CSP).
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el.hasAttribute && el.hasAttribute("data-autosubmit") && el.form) el.form.submit();
  });

  // Custom field rows on the item form.
  var add = document.getElementById("addfield");
  if (add) add.addEventListener("click", function () {
    var box = document.getElementById("fields");
    var row = document.createElement("div");
    row.className = "two field";
    row.innerHTML = '<input name="field_key" placeholder="Field" maxlength="60"><input name="field_value" placeholder="Value" maxlength="500">';
    box.appendChild(row);
    row.firstChild.focus();
  });

  // Search suggestions in the header.
  var q = document.getElementById("q"), box = document.getElementById("suggest"), timer = null;
  if (!q || !box) return;
  function hide() { box.hidden = true; box.innerHTML = ""; }
  function render(data) {
    var html = "";
    data.locations.forEach(function (l) { html += '<a href="' + l.url + '"><span class="k">Location</span>' + esc(l.name) + '</a>'; });
    data.items.forEach(function (i) { html += '<a href="' + i.url + '"><span class="k">Item</span>' + esc(i.name) + (i.where ? ' <small>' + esc(i.where) + '</small>' : '') + '</a>'; });
    if (!html) { hide(); return; }
    box.innerHTML = html; box.hidden = false;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
  q.addEventListener("input", function () {
    clearTimeout(timer);
    var v = q.value.trim();
    if (v.length < 2) { hide(); return; }
    timer = setTimeout(function () {
      fetch("/api/search?q=" + encodeURIComponent(v)).then(function (r) { return r.json(); }).then(render).catch(hide);
    }, 150);
  });
  q.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  document.addEventListener("click", function (e) { if (!box.contains(e.target) && e.target !== q) hide(); });
})();
