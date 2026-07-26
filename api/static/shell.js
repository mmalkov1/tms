/* v69: єдина оболонка — пілюля активної вкладки «переїжджає», контент
   зсувається у бік переходу. Сторінки різні, тож напрямок передається
   через sessionStorage: пілюля доїжджає вже на новій сторінці без стрибка. */
(function () {
  const KEY = 'tmsNavDir';
  const tabs = document.querySelector('header .tabs');
  if (!tabs) return;
  const links = [...tabs.querySelectorAll('a')];
  const content = document.getElementById('layout') || document.querySelector('main');

  // ---- пілюля ----
  const pill = document.createElement('span');
  pill.className = 'pill';
  tabs.insertBefore(pill, tabs.firstChild);

  function movePill(el, animate) {
    if (!el) return;
    if (!animate) pill.style.transition = 'none';
    pill.style.width = el.offsetWidth + 'px';
    pill.style.transform = 'translateX(' + (el.offsetLeft - 3) + 'px)';
    if (!animate) requestAnimationFrame(() => { pill.style.transition = ''; });
  }

  const active = tabs.querySelector('a.on');
  movePill(active, false);
  // шрифти вантажаться асинхронно — ширина вкладки може змінитись
  if (document.fonts && document.fonts.ready)
    document.fonts.ready.then(() => movePill(tabs.querySelector('a.on'), false));
  window.addEventListener('resize', () => movePill(tabs.querySelector('a.on'), true));

  // ---- вхід: напрямок від попередньої сторінки ----
  const dir = sessionStorage.getItem(KEY);
  sessionStorage.removeItem(KEY);
  if (content && dir) {
    const cls = dir === 'r' ? 'shell-inR' : 'shell-inL';
    content.classList.add(cls);
    setTimeout(() => {
      content.classList.remove(cls);
      window.dispatchEvent(new Event('resize'));   // Leaflet перерахує розміри
    }, 330);
  }

  // ---- вихід ----
  const curIdx = links.indexOf(active);
  links.forEach((a, i) => {
    const href = a.getAttribute('href');
    if (!href) return;
    a.addEventListener('click', e => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;   // нова вкладка
      e.preventDefault();
      const right = curIdx === -1 ? true : i > curIdx;
      sessionStorage.setItem(KEY, right ? 'r' : 'l');
      links.forEach(x => x.classList.remove('on'));
      a.classList.add('on');
      movePill(a, true);
      if (content) content.classList.add(right ? 'shell-outL' : 'shell-outR');
      setTimeout(() => { location.href = href; }, 200);
    });
  });
})();
