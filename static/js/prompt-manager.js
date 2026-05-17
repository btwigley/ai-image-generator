// ---------------------------------------------------------------------------
// Prompt Manager — Social Media Farm
// ---------------------------------------------------------------------------

(function () {
  'use strict';

  // --- State ---
  let characters = [];
  let currentChar = null;
  let currentCharData = null;
  let currentVariations = [];
  let currentPrompts = [];
  let currentVarIdx = 0;

  // --- DOM refs ---
  const sidebar = document.getElementById('sidebar');
  const mainContent = document.getElementById('mainContent');
  const globalProgress = document.getElementById('globalProgress');
  const globalCount = document.getElementById('globalCount');
  const searchBox = document.getElementById('searchBox');
  const newChatBtn = document.getElementById('newChatBtn');
  const resetBtn = document.getElementById('resetBtn');
  const toastEl = document.getElementById('toast');

  const EMPTY_HTML = '<div class="main-empty">Select a character from the sidebar to begin.<br>Press <span class="kbd">N</span> to jump to the next undone prompt.</div>';
  const LOADING_HTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';

  // --- localStorage helpers ---
  function storageKey(charName, varIdx) {
    return 'smf_prompt_done_' + charName + '_' + varIdx;
  }

  function isDone(charName, varIdx) {
    return localStorage.getItem(storageKey(charName, varIdx)) === '1';
  }

  function setDone(charName, varIdx, val) {
    if (val) localStorage.setItem(storageKey(charName, varIdx), '1');
    else localStorage.removeItem(storageKey(charName, varIdx));
  }

  // --- Clipboard ---
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_e) { /* noop */ }
      document.body.removeChild(ta);
      return ok;
    }
  }

  // --- Toast ---
  let toastTimer = null;
  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2000);
  }

  // --- Escape HTML ---
  function esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // --- API helpers ---
  async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error('API error: ' + res.status);
    return res.json();
  }

  // --- Init ---
  async function init() {
    mainContent.innerHTML = LOADING_HTML;
    try {
      characters = await fetchJSON('/api/characters');
      buildSidebar();
      updateProgress();
      mainContent.innerHTML = EMPTY_HTML;
    } catch (e) {
      mainContent.innerHTML = '<div class="main-empty">Failed to load characters.<br>' + esc(e.message) + '</div>';
    }
  }

  // --- Progress ---
  function getTotalVariations() {
    let total = 0;
    characters.forEach(c => { total += c.var_count; });
    return total;
  }

  function getTotalDone() {
    let done = 0;
    characters.forEach(c => {
      for (let i = 0; i < c.var_count; i++) {
        if (isDone(c.filename, i)) done++;
      }
    });
    return done;
  }

  function updateProgress() {
    const total = getTotalVariations();
    const done = getTotalDone();
    globalCount.textContent = done + '/' + total;
    globalProgress.style.width = (total ? (done / total * 100) : 0) + '%';

    // Update sidebar badges
    characters.forEach(c => {
      let charDone = 0;
      for (let i = 0; i < c.var_count; i++) {
        if (isDone(c.filename, i)) charDone++;
      }
      const badge = document.querySelector('[data-char-badge="' + c.filename + '"]');
      if (badge) badge.textContent = charDone + '/' + c.var_count;

      // Update individual variation dots
      const dots = document.querySelectorAll('[data-var-dot="' + c.filename + '"]');
      dots.forEach(dot => {
        const vi = parseInt(dot.dataset.varIdx);
        if (isDone(c.filename, vi)) {
          dot.classList.remove('pending');
          dot.classList.add('complete');
        } else {
          dot.classList.remove('complete');
          dot.classList.add('pending');
        }
      });
    });
  }

  // --- Sidebar ---
  function buildSidebar() {
    if (!characters.length) {
      sidebar.innerHTML = '<div class="no-chars">No characters found.<br>Create one in the <a href="/" style="color:var(--accent-light)">Generator</a> first.</div>';
      return;
    }

    let html = '';
    characters.forEach((c, ci) => {
      const tagsHtml = (c.tags || []).map(t => '<span>' + esc(t) + '</span>').join('');
      html += '<div class="char-group" data-char-group="' + c.filename + '">';
      html += '<div class="char-header" data-char-idx="' + ci + '">';
      html += '<span class="char-arrow" id="charArrow' + ci + '">&#9654;</span>';
      html += '<span class="char-name">' + esc(c.name) + '</span>';
      if (tagsHtml) html += '<span class="char-tags">' + tagsHtml + '</span>';
      html += '<span class="char-badge" data-char-badge="' + c.filename + '">0/' + c.var_count + '</span>';
      html += '</div>';
      html += '<div class="var-list" id="varList' + ci + '">';
      if (c.var_count === 0) {
        html += '<div class="var-item" style="color:var(--text-muted);cursor:default;font-style:italic">No variations</div>';
      }
      html += '</div></div>';
    });
    sidebar.innerHTML = html;

    sidebar.querySelectorAll('.char-header').forEach(h => {
      h.addEventListener('click', () => {
        const ci = parseInt(h.dataset.charIdx);
        toggleCharExpand(ci);
      });
    });
  }

  async function toggleCharExpand(ci) {
    const c = characters[ci];
    const listEl = document.getElementById('varList' + ci);
    const arrowEl = document.getElementById('charArrow' + ci);

    if (listEl.classList.contains('open')) {
      listEl.classList.remove('open');
      arrowEl.classList.remove('open');
      return;
    }

    // Collapse others
    document.querySelectorAll('.var-list.open').forEach(el => el.classList.remove('open'));
    document.querySelectorAll('.char-arrow.open').forEach(el => el.classList.remove('open'));

    if (c.var_count > 0 && listEl.children.length <= 1 && !listEl.dataset.loaded) {
      listEl.innerHTML = '<div class="var-item" style="color:var(--text-muted);cursor:default"><div class="spinner" style="width:14px;height:14px;border-width:2px"></div>Loading...</div>';
      listEl.classList.add('open');
      arrowEl.classList.add('open');

      try {
        const variations = await fetchJSON('/api/variations/' + c.filename);
        let vhtml = '';
        variations.forEach((v, vi) => {
          const sceneName = v.scene || 'Variation ' + (vi + 1);
          const cat = v.category || '';
          vhtml += '<div class="var-item" data-char="' + c.filename + '" data-var-idx="' + vi + '">';
          vhtml += '<span class="done-dot pending" data-var-dot="' + c.filename + '" data-var-idx="' + vi + '"></span>';
          vhtml += '<span class="scene-name">' + esc(sceneName) + '</span>';
          if (cat) vhtml += '<span class="cat-label">' + esc(cat) + '</span>';
          vhtml += '</div>';
        });
        listEl.innerHTML = vhtml || '<div class="var-item" style="color:var(--text-muted);cursor:default;font-style:italic">No variations</div>';
        listEl.dataset.loaded = '1';

        listEl.querySelectorAll('.var-item[data-char]').forEach(item => {
          item.addEventListener('click', () => {
            selectVariation(item.dataset.char, parseInt(item.dataset.varIdx));
          });
        });

        updateProgress();
      } catch (e) {
        listEl.innerHTML = '<div class="var-item" style="color:var(--red);cursor:default">Error loading</div>';
      }
    } else {
      listEl.classList.add('open');
      arrowEl.classList.add('open');
    }
  }

  // --- Select variation ---
  async function selectVariation(charFilename, varIdx) {
    document.querySelectorAll('.var-item.active').forEach(el => el.classList.remove('active'));
    const el = document.querySelector('.var-item[data-char="' + charFilename + '"][data-var-idx="' + varIdx + '"]');
    if (el) el.classList.add('active');

    if (currentChar !== charFilename) {
      currentChar = charFilename;
      mainContent.innerHTML = LOADING_HTML;

      try {
        const [charData, variations] = await Promise.all([
          fetchJSON('/api/characters/' + charFilename),
          fetchJSON('/api/variations/' + charFilename)
        ]);
        currentCharData = charData;
        currentVariations = variations;

        const prompts = await fetchJSON('/api/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            character: charData,
            variations: variations,
            limit: variations.length
          })
        });
        currentPrompts = prompts;
      } catch (e) {
        mainContent.innerHTML = '<div class="main-empty">Failed to load character data.<br>' + esc(e.message) + '</div>';
        return;
      }
    }

    currentVarIdx = varIdx;
    renderMain();
  }

  // --- Render main panel ---
  function renderMain() {
    if (!currentCharData || !currentVariations.length) {
      mainContent.innerHTML = EMPTY_HTML;
      return;
    }

    const c = currentCharData;
    const v = currentVariations[currentVarIdx];
    const prompt = currentPrompts[currentVarIdx];
    if (!v || !prompt) {
      mainContent.innerHTML = '<div class="main-empty">No prompt data for this variation.</div>';
      return;
    }

    const done = isDone(currentChar, currentVarIdx);
    const sceneName = v.scene || 'Variation ' + (currentVarIdx + 1);
    const cat = v.category || 'general';

    // Tags
    const tagsHtml = (c.tags || []).map(t => '<span>' + esc(t) + '</span>').join('');

    // Identity summary
    let identitySummary = '';
    if (c.identity) {
      const id = c.identity;
      const parts = [];
      if (id.age) parts.push('Age ' + id.age);
      if (id.hair) parts.push(id.hair);
      if (id.eyes) parts.push(id.eyes);
      if (id.skin) parts.push(id.skin);
      if (id.body) parts.push(id.body);
      if (id.face) parts.push(id.face);
      identitySummary = parts.join(' &middot; ');
    } else if (c.core) {
      identitySummary = esc(c.core.substring(0, 200)) + (c.core.length > 200 ? '...' : '');
    }

    // Variation detail chips
    const chips = [];
    if (v.outfit) chips.push({ label: 'Outfit', val: v.outfit });
    if (v.pose) chips.push({ label: 'Pose', val: v.pose });
    if (v.location) chips.push({ label: 'Location', val: v.location });
    if (v.camera) chips.push({ label: 'Camera', val: v.camera });
    if (v.emotion) chips.push({ label: 'Emotion', val: v.emotion });
    if (v.lighting) chips.push({ label: 'Lighting', val: v.lighting });
    const chipsHtml = chips.map(ch => '<span class="var-detail-chip"><strong>' + esc(ch.label) + ':</strong> ' + esc(ch.val) + '</span>').join('');

    const html = `
      <div class="char-detail-header">
        <div class="char-detail-name">${esc(c.name)}</div>
        ${tagsHtml ? '<div class="char-detail-tags">' + tagsHtml + '</div>' : ''}
      </div>

      <div class="identity-card">
        <h3>Character Identity</h3>
        <p>${identitySummary}</p>
        <button class="copy-identity-btn" id="copyIdentityBtn">Copy Identity Preamble</button>
      </div>

      <div class="variation-nav">
        <button class="nav-btn" id="prevVarBtn" ${currentVarIdx === 0 ? 'disabled' : ''}>&larr; Prev</button>
        <span class="var-indicator">${currentVarIdx + 1} / ${currentVariations.length}</span>
        <button class="nav-btn" id="nextVarBtn" ${currentVarIdx >= currentVariations.length - 1 ? 'disabled' : ''}>Next &rarr;</button>
      </div>

      ${chipsHtml ? '<div class="var-detail-row">' + chipsHtml + '</div>' : ''}

      <div class="prompt-card">
        <div class="prompt-toolbar">
          <span class="scene-label">${esc(sceneName)}</span>
          <span class="cat-badge">${esc(cat)}</span>
          <button class="copy-btn" id="copyBtn">&#128203; Copy</button>
          <button class="done-toggle ${done ? 'is-done' : 'undone'}" id="doneBtn">${done ? '&#10003; Done' : 'Mark Done'}</button>
        </div>
        <div class="prompt-text">${esc(prompt.prompt)}</div>
      </div>

      <div class="action-row">
        <button class="next-btn" id="nextUndoneBtn">Next Undone &rarr;</button>
      </div>
    `;

    mainContent.innerHTML = html;
    mainContent.scrollTop = 0;

    // Bind events
    document.getElementById('copyBtn').addEventListener('click', copyCurrentPrompt);
    document.getElementById('doneBtn').addEventListener('click', toggleDone);
    document.getElementById('nextUndoneBtn').addEventListener('click', goNextUndone);
    document.getElementById('copyIdentityBtn').addEventListener('click', copyIdentityPreamble);
    document.getElementById('prevVarBtn').addEventListener('click', () => {
      if (currentVarIdx > 0) selectVariation(currentChar, currentVarIdx - 1);
    });
    document.getElementById('nextVarBtn').addEventListener('click', () => {
      if (currentVarIdx < currentVariations.length - 1) selectVariation(currentChar, currentVarIdx + 1);
    });
  }

  // --- Copy prompt ---
  async function copyCurrentPrompt() {
    if (!currentPrompts[currentVarIdx]) return;
    const text = currentPrompts[currentVarIdx].prompt;
    const ok = await copyText(text);
    if (ok) {
      const btn = document.getElementById('copyBtn');
      btn.classList.add('copied');
      btn.innerHTML = '&#10003; Copied!';
      showToast('Prompt copied to clipboard');
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '&#128203; Copy';
      }, 1500);
    }
  }

  // --- Copy identity preamble ---
  async function copyIdentityPreamble() {
    if (!currentCharData) return;
    const c = currentCharData;
    let text = 'When I provide image generation prompts, always apply these constraints:\n\n';
    if (c.core) text += 'Character identity: ' + c.core + '\n\n';
    if (c.style) text += 'Style: ' + c.style + '\n\n';
    if (c.negative) text += 'Negative/Avoid: ' + c.negative + '\n';
    const ok = await copyText(text.trim());
    if (ok) {
      const btn = document.getElementById('copyIdentityBtn');
      btn.classList.add('copied');
      btn.textContent = 'Copied!';
      showToast('Identity preamble copied — paste into new ChatGPT chat');
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = 'Copy Identity Preamble';
      }, 1500);
    }
  }

  // --- Toggle done ---
  function toggleDone() {
    if (!currentChar) return;
    const done = isDone(currentChar, currentVarIdx);
    setDone(currentChar, currentVarIdx, !done);
    updateProgress();
    renderMain();
    highlightActiveVar();
  }

  // --- Highlight active var in sidebar ---
  function highlightActiveVar() {
    document.querySelectorAll('.var-item.active').forEach(el => el.classList.remove('active'));
    if (currentChar !== null) {
      const el = document.querySelector('.var-item[data-char="' + currentChar + '"][data-var-idx="' + currentVarIdx + '"]');
      if (el) el.classList.add('active');
    }
  }

  // --- Next undone ---
  function goNextUndone() {
    // Start from current position + 1, wrap around
    let startChar = currentChar;
    let startVar = currentVarIdx + 1;
    let found = false;

    function searchFrom(charFilename, fromVar) {
      const c = characters.find(ch => ch.filename === charFilename);
      if (!c) return false;
      for (let i = fromVar; i < c.var_count; i++) {
        if (!isDone(c.filename, i)) {
          expandAndSelect(c.filename, i);
          return true;
        }
      }
      return false;
    }

    // Search from current char, current var + 1
    if (startChar && searchFrom(startChar, startVar)) return;

    // Search remaining characters
    let pastCurrent = !startChar;
    for (const c of characters) {
      if (c.filename === startChar) { pastCurrent = true; continue; }
      if (!pastCurrent) continue;
      if (searchFrom(c.filename, 0)) return;
    }

    // Wrap around from beginning
    for (const c of characters) {
      if (c.filename === startChar) break;
      if (searchFrom(c.filename, 0)) return;
    }

    // Check current char from 0
    if (startChar && searchFrom(startChar, 0)) return;

    showToast('All prompts are done!');
  }

  async function expandAndSelect(charFilename, varIdx) {
    const ci = characters.findIndex(c => c.filename === charFilename);
    if (ci === -1) return;

    const listEl = document.getElementById('varList' + ci);
    if (!listEl.classList.contains('open')) {
      await toggleCharExpand(ci);
    }
    selectVariation(charFilename, varIdx);
  }

  // --- Search ---
  function filterSidebar(query) {
    const q = query.toLowerCase().trim();
    document.querySelectorAll('.char-group').forEach(group => {
      const name = group.querySelector('.char-name').textContent.toLowerCase();
      const tags = Array.from(group.querySelectorAll('.char-tags span')).map(s => s.textContent.toLowerCase()).join(' ');
      const visible = !q || name.includes(q) || tags.includes(q);
      group.style.display = visible ? '' : 'none';
      if (q && visible) {
        const list = group.querySelector('.var-list');
        const arrow = group.querySelector('.char-arrow');
        if (list) list.classList.add('open');
        if (arrow) arrow.classList.add('open');
      }
    });
  }

  // --- New Chat button ---
  newChatBtn.addEventListener('click', async () => {
    if (!currentCharData) {
      showToast('Select a character first');
      return;
    }
    const c = currentCharData;
    let text = 'When I provide image generation prompts, always apply these constraints:\n\n';
    if (c.core) text += 'Character identity: ' + c.core + '\n\n';
    if (c.style) text += 'Style: ' + c.style + '\n\n';
    if (c.negative) text += 'Negative/Avoid: ' + c.negative + '\n';
    const ok = await copyText(text.trim());
    if (ok) showToast('Identity + constraints copied — paste into new ChatGPT chat');
  });

  // --- Reset ---
  resetBtn.addEventListener('click', () => {
    if (!confirm('Reset ALL progress? This cannot be undone.')) return;
    characters.forEach(c => {
      for (let i = 0; i < c.var_count; i++) {
        localStorage.removeItem(storageKey(c.filename, i));
      }
    });
    updateProgress();
    if (currentChar) renderMain();
    showToast('Progress reset');
  });

  // --- Search ---
  searchBox.addEventListener('input', e => filterSidebar(e.target.value));

  // --- Keyboard shortcuts ---
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const key = e.key.toLowerCase();
    if (key === 'c') { e.preventDefault(); copyCurrentPrompt(); }
    else if (key === 'q') {
      e.preventDefault();
      if (currentCharData) {
        copyText(currentCharData.name).then(ok => {
          if (ok) showToast('"' + currentCharData.name + '" copied');
        });
      }
    }
    else if (key === 'd') { e.preventDefault(); toggleDone(); }
    else if (key === 'n') { e.preventDefault(); goNextUndone(); }
    else if (key === 'arrowleft') {
      e.preventDefault();
      if (currentChar && currentVarIdx > 0) selectVariation(currentChar, currentVarIdx - 1);
    }
    else if (key === 'arrowright') {
      e.preventDefault();
      if (currentChar && currentVarIdx < currentVariations.length - 1) selectVariation(currentChar, currentVarIdx + 1);
    }
  });

  // --- Boot ---
  init();
})();
