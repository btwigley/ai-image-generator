// ============================================
// AI Image Generator — Frontend
// ============================================

let state = {
  characters: [],
  current: null,
  currentData: null,
  variations: [],
  references: [],
  gallery: [],
  selectedImages: new Set(),
  activeTab: "identity",
  jobId: null,
  pollTimer: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function esc(str) {
  if (str == null) return "";
  const d = document.createElement("div");
  d.textContent = String(str);
  return d.innerHTML;
}

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, {
      headers: opts.body && !(opts.body instanceof FormData)
        ? { "Content-Type": "application/json" } : {},
      ...opts,
      body: opts.body instanceof FormData ? opts.body
        : opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let errData;
      try { errData = await res.json(); } catch { errData = { error: `HTTP ${res.status}` }; }
      return errData;
    }
    return res.json();
  } catch (e) {
    return { error: e.message };
  }
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  loadCharacters();
  checkApiKey();
  setupDropzone();
  setupCSVImport();

  document.querySelectorAll("#gen-model, #gen-quality, #gen-size, #gen-n")
    .forEach(el => el.addEventListener("change", updateCostEstimate));

  document.getElementById("gen-model").addEventListener("change", onModelChange);
  onModelChange();
});

function isFalModel(model) {
  return model && model.startsWith("flux-");
}

function onModelChange() {
  const model = document.getElementById("gen-model").value;
  const isFal = isFalModel(model);
  document.getElementById("quality-group").style.display = isFal ? "none" : "";
  document.getElementById("moderation-group").style.display = isFal ? "none" : "";
  updateCostEstimate();
}

async function checkApiKey() {
  const s = await api("/api/settings");
  if (s.error) return;
  const dot = document.getElementById("api-status");
  if (s.has_key) {
    dot.className = "status-dot online";
    dot.title = s.has_fal_key ? "fal.ai key set" : (s.api_key_display || "from env");
  } else {
    dot.className = "status-dot offline";
    dot.title = "No API key set";
  }
}

// ---------------------------------------------------------------------------
// Characters
// ---------------------------------------------------------------------------

async function loadCharacters() {
  const result = await api("/api/characters");
  if (Array.isArray(result)) state.characters = result;
  renderCharacterList();
}

function renderCharacterList() {
  const ul = document.getElementById("character-list");
  ul.innerHTML = "";
  for (const c of state.characters) {
    const li = document.createElement("li");
    li.className = c.filename === state.current ? "active" : "";
    li.innerHTML = `<span>${esc(c.name)}</span><span class="char-meta">${esc(c.ref_count)} refs</span>`;
    li.onclick = () => selectCharacter(c.filename);
    ul.appendChild(li);
  }
}

async function selectCharacter(filename) {
  state.current = filename;
  const charData = await api(`/api/characters/${encodeURIComponent(filename)}`);
  if (charData.error) return alert(charData.error);
  state.currentData = charData;
  const varData = await api(`/api/variations/${encodeURIComponent(filename)}`);
  state.variations = Array.isArray(varData) ? varData : [];
  const refData = await api(`/api/characters/${encodeURIComponent(filename)}/references`);
  state.references = Array.isArray(refData) ? refData : [];

  renderCharacterList();
  populateIdentityForm();
  renderReferences();
  renderVariationsTable();
  updateBadges();
  updateCostEstimate();
  updateRefToggle();
  loadGallery();

  document.getElementById("empty-state").classList.add("hidden");
  document.getElementById("editor").classList.remove("hidden");
}

function newCharacter() {
  const name = prompt("Character name:");
  if (!name) return;
  const slug = name.trim().toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_-]/g, "");
  if (!slug) return alert("Invalid name.");
  state.current = slug;
  state.currentData = {
    name: slug,
    identity: { age: "", hair: "", eyes: "", skin: "", body: "", face: "" },
    core: "",
    style: "Natural smartphone photo, believable social-media candid. Real skin texture with visible pores, slight facial asymmetry, natural body proportions, soft flyaway hairs, imperfect ambient lighting",
    negative: "watermarks, text overlays, anime style, extra fingers, distorted anatomy, airbrushed skin, CGI render",
    tags: [],
  };
  state.variations = [];
  state.references = [];

  populateIdentityForm();
  renderReferences();
  renderVariationsTable();
  updateBadges();

  document.getElementById("empty-state").classList.add("hidden");
  document.getElementById("editor").classList.remove("hidden");
  switchTab("identity");
  saveCharacter();
}

function populateIdentityForm() {
  const d = state.currentData;
  if (!d) return;
  document.getElementById("char-name").value = d.name || "";
  const id = d.identity || {};
  document.getElementById("id-age").value = id.age || "";
  document.getElementById("id-hair").value = id.hair || "";
  document.getElementById("id-eyes").value = id.eyes || "";
  document.getElementById("id-skin").value = id.skin || "";
  document.getElementById("id-body").value = id.body || "";
  document.getElementById("id-face").value = id.face || "";
  document.getElementById("char-core").value = d.core || "";
  document.getElementById("char-style").value = d.style || "";
  document.getElementById("char-negative").value = d.negative || "";
  document.getElementById("char-tags").value = (d.tags || []).join(", ");

  document.querySelectorAll("#id-age, #id-hair, #id-eyes, #id-skin, #id-body, #id-face")
    .forEach(el => {
      el.removeEventListener("input", autoBuildCore);
      el.addEventListener("input", autoBuildCore);
    });
}

function autoBuildCore() {
  const age = document.getElementById("id-age").value;
  const fields = ["id-hair", "id-eyes", "id-skin", "id-body", "id-face"]
    .map(id => document.getElementById(id).value).filter(Boolean);
  const parts = [];
  if (age) parts.push(`A ${age}-year-old person`);
  parts.push(...fields);
  if (parts.length > 0) {
    document.getElementById("char-core").value = parts.join(", ");
  }
}

function gatherCharacterData() {
  return {
    name: document.getElementById("char-name").value.trim().toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_-]/g, ""),
    identity: {
      age: document.getElementById("id-age").value,
      hair: document.getElementById("id-hair").value,
      eyes: document.getElementById("id-eyes").value,
      skin: document.getElementById("id-skin").value,
      body: document.getElementById("id-body").value,
      face: document.getElementById("id-face").value,
    },
    core: document.getElementById("char-core").value,
    style: document.getElementById("char-style").value,
    negative: document.getElementById("char-negative").value,
    tags: document.getElementById("char-tags").value.split(",").map(t => t.trim()).filter(Boolean),
  };
}

async function saveCharacter() {
  const data = gatherCharacterData();
  if (!data.name) return alert("Name is required.");
  state.currentData = data;
  state.current = data.name;
  const result = await api("/api/characters", { method: "POST", body: data });
  if (result.error) return alert(result.error);
  await loadCharacters();
  renderCharacterList();
}

async function deleteCharacter() {
  if (!state.current) return;
  if (!confirm(`Delete "${state.current}" and all its references/variations?`)) return;
  await api(`/api/characters/${encodeURIComponent(state.current)}`, { method: "DELETE" });
  state.current = null;
  state.currentData = null;
  document.getElementById("editor").classList.add("hidden");
  document.getElementById("empty-state").classList.remove("hidden");
  await loadCharacters();
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-content").forEach(tc =>
    tc.classList.toggle("active", tc.id === `tab-${tab}`));

  if (tab === "gallery") loadGallery();
  if (tab === "generate") updateCostEstimate();
}

function updateBadges() {
  document.getElementById("ref-badge").textContent = state.references.length;
  const enabled = state.variations.filter(r => r._enabled !== false).length;
  const total = state.variations.length;
  document.getElementById("var-badge").textContent = enabled === total ? total : `${enabled}/${total}`;
}

// ---------------------------------------------------------------------------
// References
// ---------------------------------------------------------------------------

function setupDropzone() {
  const dz = document.getElementById("ref-dropzone");
  const inp = document.getElementById("ref-file-input");

  dz.onclick = () => inp.click();
  inp.onchange = () => { if (inp.files.length) uploadRefs(inp.files); inp.value = ""; };

  dz.ondragover = e => { e.preventDefault(); dz.classList.add("dragover"); };
  dz.ondragleave = () => dz.classList.remove("dragover");
  dz.ondrop = e => {
    e.preventDefault();
    dz.classList.remove("dragover");
    if (e.dataTransfer.files.length) uploadRefs(e.dataTransfer.files);
  };
}

async function uploadRefs(files) {
  if (!state.current) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  await api(`/api/characters/${encodeURIComponent(state.current)}/references`, { method: "POST", body: fd });
  const refData = await api(`/api/characters/${encodeURIComponent(state.current)}/references`);
  state.references = Array.isArray(refData) ? refData : [];
  renderReferences();
  updateBadges();
  updateRefToggle();
}

function renderReferences() {
  const grid = document.getElementById("ref-grid");
  grid.innerHTML = "";
  for (const ref of state.references) {
    const card = document.createElement("div");
    card.className = "ref-card";
    const img = document.createElement("img");
    img.src = `/api/refs/${encodeURIComponent(state.current)}/${encodeURIComponent(ref.filename)}`;
    img.alt = ref.filename;
    img.loading = "lazy";
    const btn = document.createElement("button");
    btn.className = "ref-delete";
    btn.title = "Remove";
    btn.textContent = "\u00d7";
    btn.onclick = () => deleteRef(ref.filename);
    card.appendChild(img);
    card.appendChild(btn);
    grid.appendChild(card);
  }
}

async function deleteRef(filename) {
  await api(`/api/characters/${encodeURIComponent(state.current)}/references/${encodeURIComponent(filename)}`, { method: "DELETE" });
  const refData = await api(`/api/characters/${encodeURIComponent(state.current)}/references`);
  state.references = Array.isArray(refData) ? refData : [];
  renderReferences();
  updateBadges();
  updateRefToggle();
}

function updateRefToggle() {
  const section = document.getElementById("ref-toggle-section");
  if (state.references.length > 0) {
    section.classList.remove("hidden");
  } else {
    section.classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// Variations
// ---------------------------------------------------------------------------

const VAR_FIELDS = ["scene", "outfit", "pose", "location", "camera", "category", "emotion", "lighting"];

function renderVariationsTable() {
  const tbody = document.getElementById("var-tbody");
  tbody.innerHTML = "";
  for (let i = 0; i < state.variations.length; i++) {
    if (state.variations[i]._enabled === undefined) state.variations[i]._enabled = true;
    addTableRow(state.variations[i], i);
  }
  renderShotPacks();
}

function addTableRow(data, idx) {
  const tbody = document.getElementById("var-tbody");
  const enabled = data._enabled !== false;
  const tr = document.createElement("tr");
  if (!enabled) tr.classList.add("row-disabled");

  const toggleTd = document.createElement("td");
  toggleTd.className = "td-toggle";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = enabled;
  cb.className = "row-toggle";
  cb.title = enabled ? "Enabled — will generate" : "Disabled — skip this row";
  cb.onchange = () => toggleRow(idx);
  toggleTd.appendChild(cb);
  tr.appendChild(toggleTd);

  for (const field of VAR_FIELDS) {
    const td = document.createElement("td");
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = (data && data[field]) || "";
    inp.dataset.field = field;
    inp.dataset.row = idx;
    inp.oninput = () => {
      state.variations[inp.dataset.row] = state.variations[inp.dataset.row] || {};
      state.variations[inp.dataset.row][field] = inp.value;
    };
    td.appendChild(inp);
    tr.appendChild(td);
  }
  const td = document.createElement("td");
  const btn = document.createElement("button");
  btn.className = "row-delete";
  btn.textContent = "\u00d7";
  btn.onclick = () => removeRow(idx);
  td.appendChild(btn);
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function toggleRow(idx) {
  state.variations[idx]._enabled = !state.variations[idx]._enabled;
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function enableAllRows() {
  state.variations.forEach(r => r._enabled = true);
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function disableAllRows() {
  state.variations.forEach(r => r._enabled = false);
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function getEnabledVariations() {
  return state.variations.filter(r => r._enabled !== false && r.scene && r.outfit);
}

// ---------------------------------------------------------------------------
// Shot Packs (category toggles)
// ---------------------------------------------------------------------------

function renderShotPacks() {
  const container = document.getElementById("shot-packs");
  if (!container) return;
  container.innerHTML = "";

  const cats = {};
  for (const row of state.variations) {
    const cat = (row.category || "").trim().toLowerCase();
    if (!cat) continue;
    if (!cats[cat]) cats[cat] = { total: 0, enabled: 0 };
    cats[cat].total++;
    if (row._enabled !== false) cats[cat].enabled++;
  }

  const sorted = Object.keys(cats).sort();
  if (sorted.length < 2) { container.classList.add("hidden"); return; }
  container.classList.remove("hidden");

  const label = document.createElement("span");
  label.className = "shot-packs-label";
  label.textContent = "Shot Packs";
  container.appendChild(label);

  for (const cat of sorted) {
    const info = cats[cat];
    const pill = document.createElement("button");
    pill.className = "shot-pack-pill";
    if (info.enabled === info.total) pill.classList.add("active");
    else if (info.enabled > 0) pill.classList.add("partial");
    pill.textContent = `${cat} (${info.enabled}/${info.total})`;
    pill.onclick = () => toggleCategory(cat);
    container.appendChild(pill);
  }
}

function toggleCategory(cat) {
  const rows = state.variations.filter(r => (r.category || "").trim().toLowerCase() === cat);
  const allEnabled = rows.every(r => r._enabled !== false);
  for (const row of rows) row._enabled = !allEnabled;
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function enableCategory(cat) {
  for (const row of state.variations) {
    if ((row.category || "").trim().toLowerCase() === cat) row._enabled = true;
  }
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function disableCategory(cat) {
  for (const row of state.variations) {
    if ((row.category || "").trim().toLowerCase() === cat) row._enabled = false;
  }
  renderVariationsTable();
  renderShotPacks();
  updateBadges();
  updateCostEstimate();
}

function addVariationRow() {
  const empty = { _enabled: true };
  VAR_FIELDS.forEach(f => empty[f] = "");
  state.variations.push(empty);
  renderVariationsTable();
  updateBadges();
  updateCostEstimate();
}

function removeRow(idx) {
  state.variations.splice(idx, 1);
  renderVariationsTable();
  updateBadges();
  updateCostEstimate();
}

function clearVariations() {
  if (!confirm("Clear all variation rows?")) return;
  state.variations = [];
  renderVariationsTable();
  updateBadges();
  updateCostEstimate();
}

async function saveVariations() {
  if (!state.current) return;
  const toSave = state.variations
    .filter(r => r.scene && r.outfit)
    .map(r => {
      const clean = {};
      VAR_FIELDS.forEach(f => clean[f] = r[f] || "");
      return clean;
    });
  const result = await api(`/api/variations/${encodeURIComponent(state.current)}`, { method: "POST", body: toSave });
  if (result.error) return alert(result.error);
  state.variations = toSave.map(r => ({ ...r, _enabled: true }));
  renderVariationsTable();
  updateBadges();
}

function setupCSVImport() {
  document.getElementById("csv-file-input").onchange = async function () {
    if (!this.files.length || !state.current) return;
    const fd = new FormData();
    fd.append("file", this.files[0]);
    const result = await api(`/api/variations/${encodeURIComponent(state.current)}/import`, { method: "POST", body: fd });
    if (result.rows) {
      state.variations = result.rows;
      renderVariationsTable();
      updateBadges();
      updateCostEstimate();
    }
    this.value = "";
  };
}

function importCSV() {
  document.getElementById("csv-file-input").click();
}

function exportCSV() {
  const rows = state.variations.filter(r => r.scene && r.outfit);
  if (!rows.length) return;
  let csv = VAR_FIELDS.join(",") + "\n";
  for (const row of rows) {
    csv += VAR_FIELDS.map(f => {
      const v = (row[f] || "").replace(/"/g, '""');
      return v.includes(",") ? `"${v}"` : v;
    }).join(",") + "\n";
  }
  const blob = new Blob([csv], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.current || "variations"}_variations.csv`;
  a.click();
}

// ---------------------------------------------------------------------------
// Generate
// ---------------------------------------------------------------------------

async function updateCostEstimate() {
  const count = getEnabledVariations().length;
  const n = parseInt(document.getElementById("gen-n").value) || 1;
  const data = await api("/api/estimate", {
    method: "POST",
    body: {
      model: document.getElementById("gen-model").value,
      quality: document.getElementById("gen-quality").value,
      size: document.getElementById("gen-size").value,
      count: count,
      n: n,
    },
  });
  if (data.error) return;
  document.getElementById("est-rows").textContent = data.rows;
  document.getElementById("est-total").textContent = data.total_images;
  document.getElementById("est-cost").textContent = `$${data.cost_direct.toFixed(2)}`;
}

async function startGeneration() {
  if (!state.current || !state.currentData) return alert("Select a character first.");
  const valid = getEnabledVariations();
  if (!valid.length) return alert("Enable at least one variation row with scene and outfit.");

  const genSettings = {
    model: document.getElementById("gen-model").value,
    quality: document.getElementById("gen-quality").value,
    size: document.getElementById("gen-size").value,
    format: document.getElementById("gen-format").value,
    moderation: document.getElementById("gen-moderation").value,
    n: parseInt(document.getElementById("gen-n").value) || 1,
    use_references: document.getElementById("gen-use-refs")?.checked ?? true,
  };

  const charData = gatherCharacterData();
  const result = await api("/api/generate", {
    method: "POST",
    body: { character: charData, variations: valid, settings: genSettings, character_id: state.current },
  });

  if (result.error) return alert(result.error);

  state.jobId = result.job_id;
  document.getElementById("btn-generate").classList.add("hidden");
  document.getElementById("btn-cancel").classList.remove("hidden");
  document.getElementById("gen-progress").classList.remove("hidden");
  document.getElementById("gen-log").classList.remove("hidden");
  document.getElementById("log-content").textContent = "";

  pollProgress();
}

function pollProgress() {
  state.pollTimer = setInterval(async () => {
    if (!state.jobId) return;
    let res;
    try {
      res = await fetch(`/api/generate/${state.jobId}`);
    } catch {
      return;
    }
    if (!res.ok) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.jobId = null;
      document.getElementById("btn-generate").classList.remove("hidden");
      document.getElementById("btn-cancel").classList.add("hidden");
      document.getElementById("log-content").textContent = "Job lost (server may have restarted). Try generating again.";
      return;
    }
    const job = await res.json();

    const pct = job.total > 0 ? Math.round((job.completed / job.total) * 100) : 0;
    document.getElementById("progress-fill").style.width = pct + "%";
    document.getElementById("progress-text").textContent =
      `${job.completed}/${job.total} completed | ${job.failed} failed | ${job.status}`;

    if (job.log && job.log.length) {
      document.getElementById("log-content").textContent = job.log.join("\n");
    }

    if (job.status === "completed" || job.status === "cancelled" || job.status === "error") {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.jobId = null;
      document.getElementById("btn-generate").classList.remove("hidden");
      document.getElementById("btn-cancel").classList.add("hidden");

      if (job.status === "completed" && job.completed > 0) {
        loadGallery();
      }
      if (job.status === "error") {
        document.getElementById("log-content").textContent += "\n" + (job.error || "Unknown error");
      }
    }
  }, 1500);
}

async function cancelGeneration() {
  if (state.jobId) {
    await api(`/api/generate/${state.jobId}/cancel`, { method: "POST" });
  }
}

async function previewPrompts() {
  const charData = gatherCharacterData();
  const valid = getEnabledVariations();
  if (!valid.length) return alert("Enable at least one variation row first.");

  const prompts = await api("/api/preview", {
    method: "POST",
    body: { character: charData, variations: valid, limit: 5 },
  });

  if (prompts.error) return alert(prompts.error);

  const container = document.getElementById("preview-content");
  container.innerHTML = "";
  if (!Array.isArray(prompts)) return;
  for (const p of prompts) {
    const card = document.createElement("div");
    card.className = "prompt-card";
    const meta = document.createElement("div");
    meta.className = "prompt-meta";
    meta.textContent = `${p.category} | ${p.hash}`;
    const body = document.createElement("div");
    body.textContent = p.prompt;
    card.appendChild(meta);
    card.appendChild(body);
    container.appendChild(card);
  }
  document.getElementById("prompt-preview").classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Gallery
// ---------------------------------------------------------------------------

async function loadGallery() {
  const charName = state.currentData?.name || state.current;
  if (!charName) return;
  const result = await api(`/api/gallery/${encodeURIComponent(charName)}`);
  state.gallery = Array.isArray(result) ? result : [];
  state.selectedImages.clear();
  document.getElementById("gallery-badge").textContent = state.gallery.length;
  renderGallery();
}

function renderGallery() {
  const filterEl = document.getElementById("gallery-filter");
  const filterVal = filterEl.value;
  const filtered = filterVal
    ? state.gallery.filter(img => img.category === filterVal)
    : state.gallery;

  const categories = [...new Set(state.gallery.map(i => i.category))].sort();
  const currentVal = filterEl.value;
  filterEl.innerHTML = "";
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "All categories";
  filterEl.appendChild(allOpt);
  for (const cat of categories) {
    const opt = document.createElement("option");
    opt.value = cat;
    opt.textContent = cat;
    if (cat === currentVal) opt.selected = true;
    filterEl.appendChild(opt);
  }

  const grid = document.getElementById("gallery-grid");
  grid.innerHTML = "";
  for (const img of filtered) {
    const card = document.createElement("div");
    card.className = "gallery-card" + (state.selectedImages.has(img.path) ? " selected" : "");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "gallery-select";
    checkbox.checked = state.selectedImages.has(img.path);
    checkbox.onclick = (e) => {
      e.stopPropagation();
      toggleImageSelect(img.path);
    };

    const imgEl = document.createElement("img");
    imgEl.src = `/api/gallery/image/${img.path}`;
    imgEl.alt = img.filename;
    imgEl.loading = "lazy";
    imgEl.onclick = () => openLightbox(`/api/gallery/image/${img.path}`);

    const label = document.createElement("div");
    label.className = "gallery-label";
    label.textContent = `${img.category} / ${img.filename}`;

    card.appendChild(checkbox);
    card.appendChild(imgEl);
    card.appendChild(label);
    grid.appendChild(card);
  }

  document.getElementById("btn-delete-selected").classList.toggle("hidden", state.selectedImages.size === 0);
}

function toggleImageSelect(path) {
  if (state.selectedImages.has(path)) state.selectedImages.delete(path);
  else state.selectedImages.add(path);
  renderGallery();
}

async function deleteSelectedImages() {
  if (!confirm(`Delete ${state.selectedImages.size} image(s)?`)) return;
  const charName = state.currentData?.name || state.current;
  await api(`/api/gallery/${encodeURIComponent(charName)}/delete`, {
    method: "POST",
    body: { paths: [...state.selectedImages] },
  });
  await loadGallery();
}

// ---------------------------------------------------------------------------
// Lightbox
// ---------------------------------------------------------------------------

function openLightbox(src) {
  document.getElementById("lightbox-img").src = src;
  document.getElementById("lightbox").classList.remove("hidden");
}

function closeLightbox(e) {
  if (e && e.target !== e.currentTarget && e.target.tagName !== "BUTTON") return;
  document.getElementById("lightbox").classList.add("hidden");
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeLightbox();
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function showSettings() {
  document.getElementById("settings-modal").classList.remove("hidden");
}

function closeSettings() {
  document.getElementById("settings-modal").classList.add("hidden");
}

async function saveApiKey() {
  const openaiKey = document.getElementById("settings-api-key").value.trim();
  const falKey = document.getElementById("settings-fal-key").value.trim();
  const body = {};
  if (openaiKey) body.api_key = openaiKey;
  if (falKey) body.fal_key = falKey;
  if (Object.keys(body).length === 0) return;
  await api("/api/settings", { method: "POST", body });
  document.getElementById("settings-api-key").value = "";
  document.getElementById("settings-fal-key").value = "";
  closeSettings();
  checkApiKey();
}
