/* Chart builder: drag fields onto shelves, preview redraws as you go.
 *
 * Native HTML5 drag-and-drop, no library — the interaction is "pick up a chip,
 * drop it in a box", which the platform already does. Click-to-place is wired
 * alongside it so the whole thing still works on a phone, where dragging is
 * unreliable.
 *
 * The preview is drawn by charts.js's renderer against data from
 * /charts/preview, which is the same endpoint and the same engine that draws
 * saved charts on the charts page.
 */
(function () {
  const cat = JSON.parse(document.getElementById("catalogue").textContent);
  const saved = JSON.parse(document.getElementById("existing-spec").textContent);
  const templates = JSON.parse(document.getElementById("templates").textContent);
  const filterValues = JSON.parse(document.getElementById("filter-values").textContent);

  const spec = saved || {
    grain: "timeseries",
    x: null,
    measures: [],
    split: null,
    type: "line",
    bucket: "month",
    since: null,
  };

  const $ = (id) => document.getElementById(id);
  /* A daily series can be SPLIT by a holding attribute (value per ticker over
     time), so those dimensions belong in the field list even though the rest of
     the "positions" vocabulary doesn't. */
  const splittable = (f) => f.grain === "positions" && f.role === "dimension";
  const fieldsFor = (grain) =>
    cat.fields.filter((f) => f.grain === grain || (grain === "timeseries" && splittable(f)));
  const field = (key) => cat.fields.find((f) => f.key === key);

  // ---- rendering the panels ------------------------------------------- //

  function renderGrain() {
    $("grain").innerHTML = cat.grains
      .map((g) => `<option value="${g.key}">${g.label}</option>`)
      .join("");
    $("grain").value = spec.grain;
    const g = cat.grains.find((x) => x.key === spec.grain);
    $("grain-blurb").textContent = g ? g.blurb : "";
    $("bucket-wrap").style.display = spec.grain === "timeseries" ? "" : "none";
  }

  function renderTypes() {
    const usable = cat.chart_types.filter((t) => t.grains.includes(spec.grain));
    if (!usable.some((t) => t.key === spec.type)) spec.type = usable[0].key;
    $("type").innerHTML = usable
      .map((t) => `<option value="${t.key}">${t.label}</option>`)
      .join("");
    $("type").value = spec.type;
    $("bucket").innerHTML = cat.buckets
      .map((b) => `<option value="${b.key}">${b.label}</option>`)
      .join("");
    $("bucket").value = spec.bucket;
  }

  function chip(f, onRemove) {
    const li = document.createElement("li");
    li.className = "chip-field " + f.role;
    li.draggable = true;
    li.dataset.key = f.key;
    li.innerHTML = `<span>${f.label}</span>` + (onRemove ? '<button type="button" aria-label="Remove">✕</button>' : "");
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", f.key);
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    if (onRemove) li.querySelector("button").addEventListener("click", onRemove);
    else li.addEventListener("click", () => place(f.key));
    return li;
  }

  function renderFields() {
    const list = $("fieldlist");
    list.innerHTML = "";
    for (const f of fieldsFor(spec.grain)) {
      const li = chip(f, null);
      li.title = f.blurb || f.label;
      if (spec.grain === "timeseries" && splittable(f)) {
        li.classList.add("splitonly");
        li.title = `${f.label} — splits the series into one line per ${f.label.toLowerCase()}`;
      }
      list.appendChild(li);
    }
  }

  function renderShelves() {
    const fill = (id, keys, remove) => {
      const el = $(id);
      el.innerHTML = "";
      keys.filter(Boolean).forEach((k) => {
        const f = field(k);
        if (f) el.appendChild(chip(f, () => remove(k)));
      });
      if (!keys.filter(Boolean).length) {
        const hint = document.createElement("li");
        hint.className = "dropthere";
        hint.textContent = "drop a field here";
        el.appendChild(hint);
      }
    };
    fill("shelf-x", [spec.x], () => { spec.x = null; update(); });
    fill("shelf-measures", spec.measures, (k) => {
      spec.measures = spec.measures.filter((m) => m !== k);
      update();
    });
    fill("shelf-split", [spec.split], () => { spec.split = null; update(); });
  }

  /* Filters are checkbox groups rather than a free-text query: every value is
     one this portfolio actually holds, so a filter can't select nothing. */
  function renderFilters() {
    const host = $("filters");
    host.innerHTML = "";
    spec.filters = spec.filters || {};
    for (const key of cat.filterable) {
      const values = filterValues[key] || [];
      if (values.length < 2) continue;  // nothing to choose between
      const chosen = new Set(spec.filters[key] || []);
      const box = document.createElement("div");
      box.className = "filtergroup";
      const f = cat.fields.find((x) => x.key === key);
      box.innerHTML = `<span class="sub">${f ? f.label : key}</span>`;
      values.forEach((v) => {
        const id = `f-${key}-${v}`.replace(/\W/g, "-");
        const label = document.createElement("label");
        label.className = "check";
        label.innerHTML = `<input type="checkbox" id="${id}" ${chosen.has(v) ? "checked" : ""}> ${v}`;
        label.querySelector("input").addEventListener("change", (e) => {
          const set = new Set(spec.filters[key] || []);
          e.target.checked ? set.add(v) : set.delete(v);
          spec.filters[key] = [...set];
          if (!spec.filters[key].length) delete spec.filters[key];
          update();
        });
        box.appendChild(label);
      });
      host.appendChild(box);
    }
    if (!host.children.length) {
      host.innerHTML = '<p class="sub">Nothing to filter on yet.</p>';
    }
  }

  // ---- placing fields --------------------------------------------------- //

  function assign(shelf, key) {
    const f = field(key);
    if (!f) return;
    const foreign = f.grain !== spec.grain;
    // The only cross-grain move that means anything: a holding attribute
    // splitting a daily series into one line per group.
    if (foreign && !(shelf === "split" && spec.grain === "timeseries" && splittable(f))) return;

    if (shelf === "measures") {
      if (f.role !== "measure") return;
      if (!spec.measures.includes(key)) spec.measures.push(key);
    } else if (shelf === "x") {
      if (f.role !== "dimension") return;
      spec.x = key;
    } else if (shelf === "split") {
      if (f.role !== "dimension") return;
      spec.split = key;
    }
    update();
  }

  /* Click-to-place: send it wherever it fits, so the builder works without
     a mouse. */
  function place(key) {
    const f = field(key);
    if (!f) return;
    if (f.role === "measure") return assign("measures", key);
    // A holding attribute in a time-series chart can only be a split.
    if (spec.grain === "timeseries" && splittable(f)) return assign("split", key);
    assign(spec.x ? "split" : "x", key);
  }

  function wireDrops() {
    document.querySelectorAll(".shelf").forEach((shelf) => {
      const zone = shelf.querySelector(".drop");
      // The filter shelf holds checkboxes, not a drop zone. Without this guard
      // the listener setup threw and took the whole builder down with it —
      // shelves stayed empty and nothing previewed.
      if (!zone) return;
      const over = (e) => { e.preventDefault(); shelf.classList.add("over"); };
      zone.addEventListener("dragover", over);
      shelf.addEventListener("dragover", over);
      ["dragleave", "drop"].forEach((ev) =>
        shelf.addEventListener(ev, () => shelf.classList.remove("over"))
      );
      shelf.addEventListener("drop", (e) => {
        e.preventDefault();
        assign(shelf.dataset.shelf, e.dataTransfer.getData("text/plain"));
      });
    });
  }

  // ---- preview ---------------------------------------------------------- //

  let timer = null;
  let chart = null;

  function update() {
    renderShelves();
    renderTypes();
    renderFilters();
    clearTimeout(timer);
    timer = setTimeout(preview, 180);  // debounce: dragging fires a lot
  }

  function preview() {
    const msg = $("preview-msg");
    if (!spec.x || !spec.measures.length) {
      msg.textContent = "Pick an X axis and a measure.";
      if (chart) { chart.destroy(); chart = null; }
      $("t-preview").innerHTML = "";
      $("preview-plot").style.display = "";
      return;
    }
    msg.textContent = "drawing…";
    fetch("/charts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail || "can't draw that");
        return r.json();
      })
      .then((data) => {
        const n = data.labels.length;
        msg.textContent = `${n} row${n === 1 ? "" : "s"}`;
        if (chart) chart.destroy();
        // Hide the canvas for a table-only chart so the table is the preview,
        // rather than sitting under an empty plot.
        $("preview-plot").style.display = data.type === "table" ? "none" : "";
        chart = window.portfolioCharts.renderSpec("c-preview", "t-preview", data);
      })
      .catch((e) => {
        msg.textContent = e.message;
        if (chart) { chart.destroy(); chart = null; }
      });
  }

  // ---- saving ----------------------------------------------------------- //

  /* Load a template into the shelves. It becomes an ordinary draft from that
     point — save it as-is to get the default back, or change it first. */
  $("template").addEventListener("change", () => {
    const t = templates[$("template").value];
    $("template-blurb").textContent = t ? "Loaded — change anything before saving." : "";
    if (!t) return;
    Object.assign(spec, JSON.parse(JSON.stringify(t.spec)));
    spec.measures = spec.measures || [];
    window.__chartWidth = t.width;
    $("width").value = t.width;
    if (!$("chart-name").value.trim()) $("chart-name").value = t.name;
    renderGrain();
    renderTypes();
    renderFields();
    update();
  });

  $("width").addEventListener("change", () => { window.__chartWidth = $("width").value; });
  $("width").value = window.__chartWidth || "half";

  $("saveform").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("chart-name").value.trim();
    const out = $("save-msg");
    if (!name) { out.textContent = "name it first"; return; }
    out.textContent = "saving…";
    fetch("/charts/save", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({
        id: window.__chartId,
        name,
        spec,
        width: window.__chartWidth || "half",
        template_key: $("template").value || null,
      }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail || "save failed");
        return r.json();
      })
      .then((d) => {
        window.__chartId = d.id;
        out.textContent = "saved — it's on the charts page";
      })
      .catch((err) => (out.textContent = err.message));
  });

  // ---- boot ------------------------------------------------------------- //

  $("grain").addEventListener("change", () => {
    spec.grain = $("grain").value;
    // Fields belong to one grain, so switching clears the shelves rather than
    // leaving a spec that can't be drawn. Filters survive: they're about
    // holdings either way.
    spec.x = null;
    spec.measures = [];
    spec.split = null;
    renderGrain();
    renderFields();
    update();
  });
  $("type").addEventListener("change", () => { spec.type = $("type").value; update(); });
  $("bucket").addEventListener("change", () => { spec.bucket = $("bucket").value; update(); });
  $("since").addEventListener("change", () => { spec.since = $("since").value || null; update(); });
  if (spec.since) $("since").value = spec.since;

  try {
    renderGrain();
    renderTypes();
    renderFields();
    wireDrops();
  } catch (err) {
    // Never let a panel failure leave the builder blank with no explanation.
    console.error("builder setup failed", err);
    $("preview-msg").textContent = "Something went wrong setting up the builder.";
  }
  update();
})();
