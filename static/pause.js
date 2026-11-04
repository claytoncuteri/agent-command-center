/* "Go offline in N minutes": one control shared by the pages. It schedules a
   pause through /api/pause; the Claude Code hook then tells every session to
   save its work and end its turn by that time. Here: the button, the small
   form, the countdown pill and Cancel. Nothing else on the page depends on it. */
(function (root) {
  'use strict';
  var CSS = [
    '.pz { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: -4px 0 12px; font-size: 13px; }',
    '.pz button { cursor: pointer; border-radius: 99px; padding: 5px 12px; font-weight: 600; font-size: 13px;',
    '  border: 1px solid var(--hair, rgba(255,255,255,.14)); background: rgba(20,36,78,.5); color: var(--sub, #9fb0d8); }',
    '.pz button.go { color: var(--text, #eef2ff); border-color: rgba(240,211,140,.55); background: var(--gold-soft, rgba(240,211,140,.12)); }',
    '.pz input { width: 4.5em; font: inherit; font-size: 14px; color: inherit; text-align: center; padding: 4px 6px; border-radius: 8px;',
    '  border: 1px solid var(--hair, rgba(255,255,255,.14)); background: rgba(10,20,50,.6); }',
    '.pz .pill { display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px; border-radius: 99px; font-weight: 600;',
    '  color: var(--text, #eef2ff); border: 1px solid rgba(240,211,140,.55); background: var(--gold-soft, rgba(240,211,140,.12)); }',
    '.pz .pill.over { border-color: rgba(255,128,128,.5); background: var(--red-soft, rgba(255,128,128,.12)); }',
    '.pz .pill b { font-variant-numeric: tabular-nums; }',
    '.pz .note { color: var(--sub, #9fb0d8); }',
    '.pz .err { color: var(--red, #ff8c8c); }'
  ].join('\n');

  function dur(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + 's';
    var m = Math.round(s / 60);
    if (m < 60) return m + ' min';
    var h = Math.floor(m / 60); m = m % 60;
    return h + ' h' + (m ? ' ' + m + ' min' : '');
  }
  function clock(epochSeconds) {
    var d = new Date(epochSeconds * 1000);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  // opts.onChange(pause) runs after every fetch so a page can react (optional).
  function mount(el, opts) {
    opts = opts || {};
    if (!document.getElementById('pz-css')) {
      var st = document.createElement('style'); st.id = 'pz-css'; st.textContent = CSS; document.head.appendChild(st);
    }
    el.classList.add('pz');
    var pause = { at: null }, editing = false, busy = false, error = null, timer = null;

    function esc(x) { return String(x).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    function render() {
      var h;
      if (busy) {
        h = '<span class="note">One moment…</span>';
      } else if (typeof pause.at === 'number') {
        var over = pause.at * 1000 < Date.now();
        h = '<span class="pill' + (over ? ' over' : '') + '" id="pz-pill">' + pillText() + '</span>';
        h += '<span class="note">' + (over ? 'Sessions that stopped wait for you.' : 'Sessions are told to save their work and stop.') + '</span>';
        h += '<button class="go" data-act="resume">I\u2019m back: resume all</button>';
        if (!over) h += '<button data-act="cancel">Cancel</button>';
      } else if (resumeNote()) {
        h = resumeNote() + '<button data-act="open">Go offline in\u2026</button>';
      } else if (editing) {
        h = '<label>Go offline in <input id="pz-min" type="number" inputmode="numeric" min="1" max="720" value="15"> min</label>' +
            '<button class="go" data-act="start">Tell every session</button><button data-act="close">Never mind</button>';
      } else {
        h = '<button class="go" data-act="open">Go offline in…</button>';
      }
      if (error) h += '<span class="err">' + esc(error) + '</span>';
      el.innerHTML = h;
      if (editing && !busy && typeof pause.at !== 'number') {
        var inp = document.getElementById('pz-min');
        if (inp) { inp.focus(); inp.select(); }
      }
    }

    // The countdown alone changes each second: only its text is rewritten,
    // so the Cancel button beside it is never replaced under a finger.
    function pillText() {
      var left = pause.at * 1000 - Date.now();
      return left >= 0
        ? 'Offline at ' + esc(clock(pause.at)) + ' · <b>' + dur(left) + '</b> left'
        : 'Offline time was ' + esc(clock(pause.at)) + ' · <b>' + dur(-left) + '</b> ago';
    }
    // After "Resume all": what the bridge session did with it, for a while.
    function resumeNote() {
      var r = pause.resume;
      if (!r || typeof r.at !== 'number' || Date.now() - r.at * 1000 > 15 * 60 * 1000) return '';
      if (typeof r.done_at === 'number') {
        var n = typeof r.woken === 'number' ? r.woken : 0;
        return '<span class="pill">' + (n === 1 ? '1 session resumed' : n + ' sessions resumed') + ' at ' + esc(clock(r.done_at)) + '</span>';
      }
      return '<span class="pill">Resume asked at ' + esc(clock(r.at)) + '</span>' +
        '<span class="note">The bridge session wakes the ones that stopped. If nothing happens, it is not running.</span>';
    }

    function tick() {
      var pill = document.getElementById('pz-pill');
      if (!pill || typeof pause.at !== 'number') return;
      pill.innerHTML = pillText();
      pill.classList.toggle('over', pause.at * 1000 < Date.now());
    }

    function send(body, url) {
      busy = true; error = null; render();
      return fetch(url || '/api/pause', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-ACC-Pause': '1' }, body: JSON.stringify(body || {})
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error((j && j.detail) || ('HTTP ' + r.status)); },
                                        function () { throw new Error('HTTP ' + r.status); });
        return r.json();
      }).then(function (p) { pause = p || { at: null }; editing = false; })
        .catch(function (e) { error = e.message || 'Could not reach the dashboard'; })
        .then(function () { busy = false; render(); if (opts.onChange) opts.onChange(pause); });
    }

    function load() {
      return fetch('/api/pause').then(function (r) { return r.json(); }).then(function (p) {
        var before = JSON.stringify([pause.at, pause.resume || null]);
        pause = p || { at: null };
        if (before !== JSON.stringify([pause.at, pause.resume || null])) { render(); if (opts.onChange) opts.onChange(pause); }
      }).catch(function () {});
    }

    el.addEventListener('click', function (ev) {
      var b = ev.target.closest('button'); if (!b) return;
      var act = b.getAttribute('data-act');
      if (act === 'open') { editing = true; error = null; render(); }
      else if (act === 'close') { editing = false; error = null; render(); }
      else if (act === 'start') {
        var n = parseInt((document.getElementById('pz-min') || {}).value, 10);
        if (!(n >= 1 && n <= 720)) { error = 'Enter 1 to 720 minutes'; render(); return; }
        send({ minutes: n });
      } else if (act === 'cancel') send({ cancel: true });
      else if (act === 'resume') send(null, '/api/resume');
    });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && ev.target && ev.target.id === 'pz-min') {
        var go = el.querySelector('[data-act="start"]'); if (go) go.click();
      }
    });

    render(); load();
    timer = setInterval(tick, 1000);
    setInterval(load, opts.pollMs || 5000);
    return { load: load, current: function () { return pause; } };
  }

  root.ACCPause = { mount: mount, dur: dur };
}(typeof window !== 'undefined' ? window : this));
