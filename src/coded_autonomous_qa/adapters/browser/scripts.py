# ruff: noqa: E501  (embedded JavaScript)
"""JavaScript run inside the page by the direct engines (identical for Playwright and Selenium)."""

# Returns a ScreenObservation-shaped object from the live DOM.
OBSERVE_JS = r"""
(() => {
  const vis = (el) => { if (!el) return false; const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && (r.width > 0 || r.height > 0); };
  const txt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const sel = (el) => el.id ? '#' + CSS.escape(el.id) : (el.name ? `[name="${el.name.replace(/"/g, '\\"')}"]` : null);
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return txt(el.labels[0]).replace(/\s*\*\s*$/, '');
    const al = el.getAttribute('aria-label'); if (al) return al;
    const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = document.getElementById(lb); if (t) return txt(t); }
    return null; };
  const marker = (el) => !!(el.labels && el.labels.length && /\*|required/i.test(txt(el.labels[0])));
  const fields = []; const seen = new Set();
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text'));
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    const name = el.name || el.id || labelOf(el) || el.placeholder; if (!name) continue;
    const key = name + '|' + type; if (type !== 'radio' && seen.has(key)) continue; seen.add(key);
    const num = (v) => (v === '' || v === null || isNaN(Number(v))) ? null : Number(v);
    const ml = el.maxLength; const mn = el.minLength;
    fields.push({ name, label: labelOf(el), type, required: el.required || el.getAttribute('aria-required') === 'true',
      required_marker: marker(el), min_length: (mn && mn > 0) ? mn : null, max_length: (ml && ml > 0 && ml < 524288) ? ml : null,
      min: num(el.getAttribute('min')), max: num(el.getAttribute('max')), pattern: el.getAttribute('pattern'),
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => txt(o)).filter(Boolean) : [],
      placeholder: el.placeholder || null, enabled: !el.disabled, visible: vis(el), selector: sel(el),
      validation_messages: (el.validationMessage && el.getAttribute('aria-invalid') === 'true') ? [el.validationMessage] : [] });
  }
  const controls = [];
  for (const el of document.querySelectorAll('button, a[href], input[type=submit], input[type=button], [role=button], [role=link], [role=tab], [role=menuitem]')) {
    if (!vis(el)) continue;
    const label = txt(el) || el.value || el.getAttribute('aria-label'); if (!label) continue;
    const role = el.getAttribute('role');
    const kind = role === 'tab' ? 'tab' : role === 'menuitem' ? 'menu_item' : (el.tagName === 'A' || role === 'link') ? 'link' : 'button';
    controls.push({ label: label.slice(0, 300), kind, href: el.tagName === 'A' ? el.getAttribute('href') : null,
      selector: el.id ? '#' + CSS.escape(el.id) : null, enabled: !el.disabled });
  }
  const messages = Array.from(document.querySelectorAll('[role=alert], [aria-live], .error, .error-message, .invalid-feedback, .alert, .message, .toast'))
    .filter(vis).map(txt).filter(Boolean).slice(0, 30);
  return { url: location.href, title: document.title, headings: Array.from(document.querySelectorAll('h1,h2,h3')).map(txt).filter(Boolean).slice(0, 20),
    fields, controls, messages, visible_text_excerpt: txt(document.body).slice(0, 8000) };
})()
"""

# Takes an element, returns its validation/visibility state.
FIELD_STATE_JS = r"""
(el) => {
  const s = getComputedStyle(el); const r = el.getBoundingClientRect();
  return { tag: el.tagName.toLowerCase(), value: ('value' in el) ? String(el.value) : null,
    valid: el.validity ? el.validity.valid : null, value_missing: el.validity ? el.validity.valueMissing : null,
    validation_message: el.validationMessage || '', aria_invalid: el.getAttribute('aria-invalid'),
    required: !!el.required || el.getAttribute('aria-required') === 'true', disabled: !!el.disabled,
    visible: s.visibility !== 'hidden' && s.display !== 'none' && (r.width > 0 || r.height > 0) };
}
"""

BODY_TEXT_JS = "(() => (document.body ? document.body.innerText : '') )()"
