/* v75: пошук на карті — заявки дня (локально), адреса через геокодер, координати.
   Підключається на Плануванні та План/Факт; відрізняється лише джерелом точок.

   MapSearch.init({
     map,                       // Leaflet-карта
     getItems: () => [ {client, address, seq, label, color, lat, lon, pick} ],
     onGeo: (lat, lon, label)   // необовʼязково: своя реакція на знайдену адресу
   });
*/
window.MapSearch = (function () {
  const ICO_SEARCH = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>';
  const ICO_PIN = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/></svg>';

  /* Google копіює "50.41502447868642, 30.519924158561874" — крапка десяткова,
     кома між парою. Українська локаль дає "50,415024 30,519924" — навпаки.
     Порядок перевірок важливий: інакше формат Google прочитається як одне число. */
  function parseCoords(raw) {
    const s = (raw || '').trim().replace(/\s+/g, ' ');
    let m = s.match(/^(-?\d{1,3}\.\d+)\s*[,;]\s*(-?\d{1,3}\.\d+)$/);      // Google
    if (!m) m = s.match(/^(-?\d{1,3}[.,]\d+)[\s;]+(-?\d{1,3}[.,]\d+)$/);  // кома десяткова
    if (!m) m = s.match(/^(-?\d{1,3})\s*[,;]\s*(-?\d{1,3})$/);            // цілі
    if (!m) return null;
    const lat = parseFloat(m[1].replace(',', '.'));
    const lon = parseFloat(m[2].replace(',', '.'));
    if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    return { lat, lon };
  }

  function init(cfg) {
    const map = cfg.map;
    const host = map.getContainer();
    let tempMarker = null, open = false;

    const box = document.createElement('div');
    box.className = 'msearch';
    box.innerHTML = `
      <button class="ms-btn" title="Пошук на карті (Ctrl+F)">${ICO_SEARCH}</button>
      <div class="ms-box">
        <div class="ms-in">${ICO_SEARCH}
          <input type="text" placeholder="Клієнт, адреса або координати" autocomplete="off">
          <button class="ms-x" title="Закрити">&times;</button>
        </div>
        <div class="ms-out"></div>
      </div>`;
    host.appendChild(box);
    if (window.L && L.DomEvent) {
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
    }

    const btn = box.querySelector('.ms-btn');
    const inp = box.querySelector('input');
    const out = box.querySelector('.ms-out');

    function clearTemp() {
      if (tempMarker) { map.removeLayer(tempMarker); tempMarker = null; }
    }
    function dropTemp(lat, lon, label) {
      clearTemp();
      tempMarker = L.marker([lat, lon], {
        icon: L.divIcon({
          className: '',
          html: `<div class="ms-tmp"><span>${label}</span>
                 <svg width="26" height="26" viewBox="0 0 24 24" fill="#00356B" stroke="#fff" stroke-width="1.5"><path d="M12 22s8-6 8-12a8 8 0 1 0-16 0c0 6 8 12 8 12z"/><circle cx="12" cy="10" r="2.6" fill="#fff" stroke="none"/></svg></div>`,
          iconSize: null, iconAnchor: [13, 30]
        })
      }).addTo(map);
      map.flyTo([lat, lon], Math.max(map.getZoom(), 15), { duration: .5 });
    }

    function hintHtml() {
      return `<div class="ms-hint">Почніть вводити назву клієнта чи адресу.<br>
        Enter — пошук адреси через геокодер.<br>
        Координати з Google (<code>50.415024, 30.519924</code>) розпізнаються одразу.</div>`;
    }

    function render() {
      const q = inp.value.trim();
      if (!q) { out.innerHTML = hintHtml(); clearTemp(); return; }

      const c = parseCoords(q);
      if (c) {
        out.innerHTML = `<div class="ms-found"><b>Координати розпізнано</b>
          <span>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}</span></div>`;
        dropTemp(c.lat, c.lon, c.lat.toFixed(5) + ', ' + c.lon.toFixed(5));
        return;
      }

      const t = q.toLowerCase();
      let items = [];
      try { items = cfg.getItems() || []; } catch (e) { items = []; }
      const hits = items.filter(it =>
        ((it.client || '') + ' ' + (it.address || '') + ' ' + (it.doc || '')).toLowerCase().includes(t)
      ).slice(0, 40);

      let html = '';
      if (hits.length) {
        html += '<div class="ms-res">' + hits.map((it, i) => `
          <div class="ms-row" data-i="${i}">
            <span class="ms-seq" style="${it.seq ? '' : 'background:#77736D'}">${it.seq || '—'}</span>
            <span class="ms-main"><b>${it.client || '—'}</b><span>${it.address || ''}</span></span>
            ${it.label ? `<span class="ms-tag" style="background:${(it.color || '#00356B')}22;color:${it.color || '#00356B'}">${it.label}</span>` : ''}
          </div>`).join('') + '</div>';
      } else {
        html += '<div class="ms-hint">Серед заявок цього дня збігів немає.</div>';
      }
      html += `<div class="ms-act">${ICO_PIN}
        <span><b>Знайти адресу «${q.length > 28 ? q.slice(0, 28) + '…' : q}»</b>
        <span>через геокодер · Enter</span></span></div>`;
      out.innerHTML = html;

      out.querySelectorAll('.ms-row').forEach(row => {
        row.addEventListener('click', () => {
          const it = hits[+row.dataset.i];
          if (!it) return;
          clearTemp();
          if (typeof it.pick === 'function') it.pick();
          else if (it.lat) map.flyTo([+it.lat, +it.lon], Math.max(map.getZoom(), 15), { duration: .5 });
          out.innerHTML = `<div class="ms-found"><b>${it.client || ''}</b>
            <span>${it.label ? it.label + ' · ' : ''}показано на карті</span></div>`;
        });
      });
      const act = out.querySelector('.ms-act');
      if (act) act.addEventListener('click', geocode);
    }

    async function geocode() {
      const q = inp.value.trim();
      if (!q) return;
      if (parseCoords(q)) return render();
      out.innerHTML = '<div class="ms-hint">Шукаю адресу через геокодер…</div>';
      try {
        const r = await fetch('/api/geocode-address', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address: q })
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          out.innerHTML = `<div class="ms-hint" style="color:#B8860B">${j.detail || 'Адресу не знайдено'}</div>`;
          return;
        }
        out.innerHTML = `<div class="ms-found"><b>Знайдено</b>
          <span>${j.lat.toFixed(6)}, ${j.lon.toFixed(6)} — тимчасовий маркер</span></div>`;
        if (cfg.onGeo) cfg.onGeo(j.lat, j.lon, q);
        dropTemp(j.lat, j.lon, q.length > 32 ? q.slice(0, 32) + '…' : q);
      } catch (e) {
        out.innerHTML = '<div class="ms-hint" style="color:#B8860B">Немає звʼязку з сервером</div>';
      }
    }

    function show() {
      open = true; box.classList.add('open');
      inp.focus(); inp.select(); render();
    }
    function hide() {
      open = false; box.classList.remove('open');
      inp.value = ''; out.innerHTML = ''; clearTemp();
    }

    btn.addEventListener('click', show);
    box.querySelector('.ms-x').addEventListener('click', hide);
    inp.addEventListener('input', render);
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); geocode(); }
      if (e.key === 'Escape') { e.preventDefault(); hide(); }
    });

    // Ctrl+F перехоплюємо лише коли курсор над картою або пошук уже відкритий,
    // щоб не ламати звичайний пошук браузера на решті сторінки
    let overMap = false;
    host.addEventListener('mouseenter', () => { overMap = true; });
    host.addEventListener('mouseleave', () => { overMap = false; });
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F') && (overMap || open)) {
        e.preventDefault(); show();
      }
    });

    return { show, hide, parseCoords };
  }

  return { init, parseCoords };
})();
