// The landing page's one script (templates/accounts/landing.html), loaded with defer. No library.
// Everything here is an enhancement: without it every section, panel and picture shows, and every
// link works. Under prefers-reduced-motion nothing loops, pins or follows the pointer.
(function () {
  var doc = document.documentElement;
  var io = 'IntersectionObserver' in window;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

  // Analytics: one GA4 event for every call to action, named by data-cta and placed by
  // data-cta-location (hero, nav, menu, pricing, final). Nothing is sent when gtag isn't loaded.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-cta]');
    if (!a || typeof window.gtag !== 'function') return;
    var params = { cta: a.getAttribute('data-cta'), location: a.getAttribute('data-cta-location') || '' };
    if (a.hasAttribute('data-cta-plan')) params.plan = a.getAttribute('data-cta-plan');
    window.gtag('event', 'cta_click', params);
  });

  // Blocks rise in once as they come into view (landing.css: html.js .lp-reveal). The hidden start
  // state is switched on here, with the observer that reveals it, so nothing can stay invisible.
  if (io) {
    doc.classList.add('js');
    var rio = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); rio.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -15% 0px', threshold: 0 });
    document.querySelectorAll('.lp-reveal').forEach(function (el) { rio.observe(el); });
  }

  // Mobile menu: a real button with aria-expanded; closes on a link choice or Escape.
  var header = document.querySelector('.lp-header');
  var menuButton = document.getElementById('menu-button');
  if (header && menuButton) {
    var setMenu = function (open) {
      header.classList.toggle('menu-open', open);
      menuButton.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    menuButton.addEventListener('click', function () { setMenu(menuButton.getAttribute('aria-expanded') !== 'true'); });
    // Collapse instantly on a link choice: a panel still animating shut would shift the page
    // under the smooth scroll and land it past the section.
    document.querySelectorAll('#mobile-nav a').forEach(function (a) {
      a.addEventListener('click', function () {
        header.classList.add('instant'); setMenu(false);
        void header.offsetHeight; header.classList.remove('instant');
      });
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && header.classList.contains('menu-open')) { setMenu(false); menuButton.focus(); }
    });
  }

  // Scroll-spy: a nav link is marked while its section spans the line 30% down the viewport, and
  // none is marked between them (trust, FAQ), so the highlight never names the wrong section.
  var links = Array.prototype.slice.call(document.querySelectorAll('nav[aria-label="Primary"] a[href^="#"]'));
  var spied = links.map(function (a) { return document.querySelector(a.getAttribute('href')); });
  var spyQueued = false;
  function spy() {
    spyQueued = false;
    var line = window.innerHeight * 0.3;
    links.forEach(function (a, i) {
      var s = spied[i], r = s && s.getBoundingClientRect();
      var on = !!r && r.top <= line && r.bottom > line;
      a.classList.toggle('active', on);
      if (on) { a.setAttribute('aria-current', 'true'); } else { a.removeAttribute('aria-current'); }
    });
  }
  if (links.length) {
    var queueSpy = function () { if (!spyQueued) { spyQueued = true; requestAnimationFrame(spy); } };
    window.addEventListener('scroll', queueSpy, { passive: true });
    window.addEventListener('resize', queueSpy);
    // Sections below the hero are drawn as they near the screen (content-visibility), which can
    // change their height without a scroll event; re-check whenever any section resizes.
    if ('ResizeObserver' in window) {
      var ro = new ResizeObserver(queueSpy);
      document.querySelectorAll('main > section').forEach(function (s) { ro.observe(s); });
    }
    spy();
  }

  // WAI-ARIA tabs: the platform tabs and the developers' code samples. Each row is hidden in the
  // markup so that, without this script, every panel shows; here it appears and one panel stays
  // open. Returns the tabs and their select function.
  function setUpTabs(tablist) {
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
    var panelOf = function (tab) { return document.getElementById(tab.getAttribute('aria-controls')); };
    var select = function (tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        panelOf(t).hidden = !on;
      });
      if (focus) tab.focus();
    };
    tablist.hidden = false;
    select(tabs[0], false);
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t, false); });
      t.addEventListener('keydown', function (e) {
        var to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
        if (to === undefined) return;
        e.preventDefault();
        select(tabs[(to + tabs.length) % tabs.length], true);
      });
    });
    return { tabs: tabs, select: select };
  }
  var codeTabs = document.querySelector('.lp-ctabs');
  if (codeTabs) setUpTabs(codeTabs);

  // Platform tabs. A link to a panel's id (the old #content-studio) opens that tab.
  var tablist = document.querySelector('.lp-ptabs');
  if (tablist) {
    var platform = setUpTabs(tablist);
    var tabs = platform.tabs, select = platform.select;
    var openFromHash = function () {
      var id = location.hash.slice(1);
      var tab = id && tabs.filter(function (t) { return t.getAttribute('aria-controls') === id; })[0];
      if (!tab) return;
      select(tab, false);
      tablist.scrollIntoView();
    };
    window.addEventListener('hashchange', openFromHash);
    // On arrival the browser jumps to the fragment itself, after this script; land on the tab
    // row (not the panel's top edge, which would hide the tabs) once the page has loaded.
    if (document.readyState === 'complete') { openFromHash(); } else { window.addEventListener('load', openFromHash); }
  }

  // How it works as a pinned scene: on wide screens the pictures move into one sticky frame
  // beside the steps, and the step crossing the middle of the viewport shows its picture. On
  // phones, under reduced motion and without this script it stays a stacked list.
  var how = document.querySelector('.lp-how');
  var wide = window.matchMedia('(min-width: 1024px)');
  if (how && io) {
    var grid = how.querySelector('.lp-how-grid');
    var steps = Array.prototype.slice.call(how.querySelectorAll('.lp-step'));
    var arts = steps.map(function (s) { return s.querySelector('.lp-step-art'); });
    var stage = document.createElement('div');
    stage.className = 'lp-how-stage';
    var activate = function (step) {
      steps.forEach(function (s, i) {
        var on = s === step;
        s.classList.toggle('is-active', on);
        if (arts[i]) arts[i].classList.toggle('is-active', on);
      });
    };
    var sio = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) activate(e.target); });
    }, { rootMargin: '-50% 0px -50% 0px' });
    var layout = function () {
      var pin = wide.matches && !reduce.matches;
      if (pin === how.classList.contains('is-pinned')) return;
      how.classList.toggle('is-pinned', pin);
      if (pin) {
        arts.forEach(function (art) { if (art) stage.appendChild(art); });
        grid.appendChild(stage);
        activate(steps[0]);
        steps.forEach(function (s) { sio.observe(s); });
      } else {
        sio.disconnect();
        steps.forEach(function (s, i) { if (arts[i]) s.appendChild(arts[i]); });
        if (stage.parentNode) stage.parentNode.removeChild(stage);
      }
    };
    layout();
    wide.addEventListener('change', layout);
    reduce.addEventListener('change', layout);
  }

  // The hero scene's loop: hold the final frame, then replay the five steps (a message arrives, AI
  // drafts the reply, interested, the order, the follow-up) and hold again. It starts after the
  // page has loaded, runs only while on screen and the tab is visible, and never under reduced
  // motion, where the final frame simply stays.
  var scene = document.querySelector('.lp-scene');
  if (scene && io) {
    var STEPS = [500, 1700, 3100, 4500, 5900]; // ms after a replay starts
    var HOLD = 3200;                            // final frame before the next replay
    var timers = [], visible = false, started = false;
    var clear = function () { timers.forEach(clearTimeout); timers = []; };
    var reset = function () { clear(); scene.classList.remove('play', 's1', 's2', 's3', 's4', 's5'); };
    var replay = function () {
      scene.classList.remove('s1', 's2', 's3', 's4', 's5');
      scene.classList.add('play');
      STEPS.forEach(function (ms, i) { timers.push(setTimeout(function () { scene.classList.add('s' + (i + 1)); }, ms)); });
      timers.push(setTimeout(replay, STEPS[STEPS.length - 1] + HOLD));
    };
    var run = function () {
      reset();
      if (!started || !visible || document.hidden || reduce.matches) return;
      timers.push(setTimeout(replay, HOLD));
    };
    new IntersectionObserver(function (entries) { visible = entries[0].isIntersecting; run(); }).observe(scene);
    document.addEventListener('visibilitychange', run);
    reduce.addEventListener('change', run);
    var begin = function () { started = true; run(); };
    if (document.readyState === 'complete') { begin(); } else { window.addEventListener('load', begin); }
  }

  // A soft light that follows the pointer across cards (fine pointers only, never under reduced
  // motion). The CSS draws it from --mx/--my; without them it sits off the card.
  if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
    document.addEventListener('pointermove', function (e) {
      if (reduce.matches) return;
      var card = e.target.closest && e.target.closest('.lp-card, .lp-trust > div, .lp-trust > a');
      if (!card) return;
      var r = card.getBoundingClientRect();
      card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      card.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }, { passive: true });
  }
})();
