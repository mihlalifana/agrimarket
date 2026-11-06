/* AgriMarket page helpers. */
(function () {
  // Phone menu button; desktop 'More' closes when clicking elsewhere.
  var mt = document.querySelector('.menu-toggle');
  if (mt) mt.addEventListener('toggle', function () { document.querySelector('.topbar').classList.toggle('menu-open', mt.open); });
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.more[open]').forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });
  // One-time token on every POST form so the same data cannot be saved twice.
  function token() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }
  document.querySelectorAll('form[method="post"]:not([data-external]), form[method="POST"]:not([data-external])').forEach(function (f) {
    if (!f.querySelector('input[name="_ftoken"]')) {
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = '_ftoken'; i.value = token();
      f.appendChild(i);
    }
    // Block a second click while the first submit is on its way.
    f.addEventListener('submit', function (e) {
      if (f.dataset.sending === '1') { e.preventDefault(); return; }
      if (e.defaultPrevented) return;
      f.dataset.sending = '1';
      f.querySelectorAll('button, input[type=submit]').forEach(function (b) { b.classList.add('is-sending'); });
      setTimeout(function () { f.dataset.sending = ''; f.querySelectorAll('.is-sending').forEach(function (b) { b.classList.remove('is-sending'); }); }, 8000);
    });
  });

  // Messages fade after a while so they don't push the page down for long.
  document.querySelectorAll('.messages li:not(.error):not(.stay)').forEach(function (li) {
    setTimeout(function () { li.style.transition = 'opacity .4s'; li.style.opacity = '0';
      setTimeout(function () { li.remove(); }, 450); }, 6000);
  });

  // Panels: <details data-open-on-error> stays open when it contains form errors.
  document.querySelectorAll('details').forEach(function (d) {
    if (d.querySelector('.errorlist, .nonfield')) d.open = true;
  });
  // Open a panel from a link: href="#add-planting"
  function openHash() {
    if (!location.hash) return;
    var el = document.getElementById(location.hash.slice(1));
    if (el && el.tagName === 'DETAILS') { el.open = true; el.scrollIntoView({block: 'start'}); }
  }
  window.addEventListener('hashchange', openHash); openHash();

  // Highlight the row just saved: ?new=<id> marks <tr data-id="id">
  var params = new URLSearchParams(location.search);
  var nid = params.get('new');
  if (nid) {
    var row = document.querySelector('[data-id="' + CSS.escape(nid) + '"]');
    if (row) { row.classList.add('just-saved'); row.scrollIntoView({block: 'center'}); }
  }
})();

/* Product forms: a form with data-cmap="<json script id>" limits the unit and
   grade lists to the chosen product's official values, and shows the market
   price guide when data-guide-url is set. Works for several forms on one page. */
(function () {
  const num2 = v => { const n = Number(v); const [i, d] = Math.abs(n).toFixed(2).split('.'); return (n < 0 ? '-' : '') + i.replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + '.' + d; };
  const money = v => 'R ' + num2(v);
  document.querySelectorAll('form[data-cmap]').forEach(function (form) {
    const src = document.getElementById(form.dataset.cmap);
    if (!src) return;
    const map = JSON.parse(src.textContent);
    const pick = n => form.querySelector('select[name$="' + n + '"]:not([type=hidden])');
    const c = pick('commodity'), unit = pick('unit'), grade = pick('grade');
    if (!c) return;
    const allUnits = unit ? Array.from(unit.options).map(o => [o.value, o.text]) : [];
    const anyLabel = form.dataset.gradeAny || 'Choose grade';
    const ref = form.querySelector('.std-ref'), guide = form.querySelector('.price-guide');
    function loadGuide() {
      if (!guide || !form.dataset.guideUrl) return;
      if (!c.value || !unit) { guide.hidden = true; return; }
      const g = grade && !grade.disabled ? grade.value : '';
      fetch(form.dataset.guideUrl + '?commodity=' + c.value + '&unit=' + unit.value + '&grade=' + encodeURIComponent(g))
        .then(r => r.json()).then(d => {
          guide.hidden = false;
          if (!d.guide) { guide.textContent = 'Price guide: not enough recent sales from at least ' + d.min_farms + ' farms for this product yet.'; return; }
          const x = d.guide;
          guide.innerHTML = '<strong>Market price (' + x.scope + ', last 90 days):</strong> average ' + money(x.avg) +
            ' per ' + unit.value + ' · range ' + money(x.low) + ' – ' + money(x.high) + ' · ' + x.farms + ' farms';
        }).catch(() => { guide.hidden = true; });
    }
    function sync() {
      const info = map[c.value];
      if (unit && info && !unit.disabled) {
        const keep = unit.value;
        unit.innerHTML = '';
        allUnits.filter(([v]) => info.units.includes(v)).forEach(([v, t]) => unit.add(new Option(t, v)));
        if (info.units.includes(keep)) unit.value = keep;
      }
      if (grade && !grade.disabled) {
        const keep = grade.value;
        const list = info ? info.grades : [];
        grade.innerHTML = '';
        grade.add(new Option(!c.value ? 'Choose a product first' : (list.length ? anyLabel : 'No grade table for this product'), ''));
        list.forEach(g => grade.add(new Option(g, g)));
        if (list.includes(keep)) grade.value = keep;
        grade.disabled = !!c.value && !list.length;
      }
      if (ref) ref.textContent = info && info.ref ? 'Standard: ' + info.ref : '';
      loadGuide();
    }
    c.addEventListener('change', sync);
    if (unit) unit.addEventListener('change', loadGuide);
    if (grade) grade.addEventListener('change', loadGuide);
    sync();
  });
})();

/* Spray forms: only list verified products registered for the chosen crop, and show the label's
   pre-harvest interval. The server checks the same thing again when the form is saved. */
(function () {
  document.querySelectorAll('script.remedy-map').forEach(function (src) {
    const form = src.closest('form'); if (!form) return;
    const map = JSON.parse(src.textContent);
    const crop = form.querySelector('select[name$="planting"]');
    const remedy = form.querySelector('select[name$="remedy"]');
    const hint = form.querySelector('.remedy-label');
    if (!remedy) return;
    const all = Array.from(remedy.options).map(o => [o.value, o.text]);
    const key = () => crop ? crop.value : Object.keys(map)[0];
    function showHint() {
      const t = (map[key()] || {})[remedy.value];
      if (hint) { hint.hidden = !t; hint.textContent = t || ''; }
    }
    function filter() {
      const ok = map[key()] || {};
      const keep = remedy.value;
      remedy.innerHTML = '';
      all.filter(([v]) => v === '' || ok[v]).forEach(([v, t]) => remedy.add(new Option(t, v)));
      if (!crop || crop.value) {
        if (remedy.options.length <= 1) remedy.options[0].text = 'No verified product registered for this crop';
      } else if (remedy.options.length) {
        remedy.options[0].text = 'Choose the crop first';
      }
      if (ok[keep]) remedy.value = keep;
      showHint();
    }
    if (crop) crop.addEventListener('change', filter);
    remedy.addEventListener('change', showHint);
    filter();
  });
})();
