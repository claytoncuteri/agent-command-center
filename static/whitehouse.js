/*
 * White House view: the building model.
 *
 * Turns the session model (sessions.js deriveSessions) into departments: one
 * per project, with its secretary and one staffer per session. Decides who
 * walks to the Oval Office (a decision), what waits on the desk list (an
 * update), and what finished today. Pure functions, no DOM, so the rules are
 * tested in Node the same way sessions.js is.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ACCWhiteHouse = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var OFFICES_MAX = 16;       // drawn offices; the rest are listed in the Annex
  var DESKS_PER_OFFICE = 4;   // drawn staffers per office; the panel lists them all
  var TODAY_MS = 24 * 60 * 60 * 1000;
  var RECENT_MS = TODAY_MS;   // staff quiet longer than this keep no desk; offices with none go to the Annex

  // A session's standing in the building.
  //   decision: a permission or question is open, or its last turn ended with a question
  //   update:   its last turn ended with a recap and no question (nobody walks)
  //   working / turn / paused / idle / closed: as the session view has them
  function standing(s) {
    if (!s) return 'idle';
    if (s.state === 'need' || s.state === 'error') return 'decision';
    if (s.state === 'work' || s.state === 'quiet') return 'working';
    if (s.state === 'closed') return 'closed';
    if (s.state === 'paused') return 'paused';
    if (s.state === 'idle') return 'idle';
    // 'turn': finished and waiting for the next message.
    if (s.summary && s.summary.ask) return 'decision';
    if (s.summary && s.summary.recap) return 'update';
    return 'turn';
  }

  // The one line a memo leads with, in the session's own words when it has them.
  function need(s) {
    var st = standing(s);
    if (st === 'decision') {
      if (s.state === 'need' && s.ask) return (s.ask.kind || 'Approval') + ': ' + (s.ask.text || 'waiting on you');
      if (s.state === 'error') return 'Something went wrong; it needs a look';
      return s.summary && s.summary.ask ? s.summary.ask : 'A decision';
    }
    if (st === 'update') return 'Nothing. For your information.';
    return null;
  }

  function finishedToday(s, now) {
    if (!s.summary || !s.summary.recap) return false;
    var at = s.summary.turnAt || s.lastAt;
    return typeof at === 'number' && now - at <= TODAY_MS;
  }

  // Departments from the session list: one per project, sorted so the ones
  // that need you come first, then the busiest.
  function buildDepartments(sessions, now) {
    now = typeof now === 'number' ? now : Date.now();
    var byName = {};
    (sessions || []).forEach(function (s) {
      if (!s || s.state === 'closed') return;
      var name = s.project || s.folder || 'Unknown';
      var d = byName[name] = byName[name] || { name: name, staff: [], decisions: 0, updates: 0, working: 0, idle: 0 };
      var st = standing(s);
      var recent = st !== 'idle' || now - (s.lastAt || 0) <= RECENT_MS;
      d.staff.push({ key: s.key, session: s.session, title: s.title, standing: st, need: need(s), recent: recent, s: s });
      if (st === 'decision') d.decisions += 1;
      else if (st === 'update') d.updates += 1;
      else if (st === 'working') d.working += 1;
      else if (st === 'idle' || st === 'paused') d.idle += 1;
    });
    var order = { decision: 0, update: 1, working: 2, turn: 3, paused: 4, idle: 5, closed: 6 };
    var list = Object.keys(byName).map(function (n) { return byName[n]; });
    list.forEach(function (d) {
      d.staff.sort(function (a, b) {
        if (order[a.standing] !== order[b.standing]) return order[a.standing] - order[b.standing];
        return (b.s.lastAt || 0) - (a.s.lastAt || 0);
      });
      // The secretary walks to the Oval Office when any staffer brings a decision.
      d.inOval = d.decisions > 0;
      d.oldestDecision = d.staff.filter(function (x) { return x.standing === 'decision'; })
        .reduce(function (m, x) { var t = x.s.since || x.s.lastAt || 0; return m === null || t < m ? t : m; }, null);
    });
    // Offices keep their places as sessions come and go: alphabetical, so a
    // department is always where you last saw it. Urgency lives on the desk lists.
    list.sort(function (a, b) {
      var x = a.name.toLowerCase(), y = b.name.toLowerCase();
      return x < y ? -1 : x > y ? 1 : 0;
    });
    // Desks go to staff active in the last day (idle ones sort last, so the
    // drawn desks are the recent ones). An office with nobody recent and no
    // decision is not drawn; the Annex lists it and the panels keep everyone.
    var slot = 0;
    list.forEach(function (d) {
      d.active = d.staff.filter(function (x) { return x.recent; }).length;
      d.desks = Math.min(d.active, DESKS_PER_OFFICE);
      d.drawn = (d.active > 0 || d.inOval) && slot < OFFICES_MAX;
      d.index = d.drawn ? slot++ : -1;
    });
    return list;
  }

  // The desk lists: decisions oldest first, updates and finished newest first.
  function deskLists(departments, now) {
    now = typeof now === 'number' ? now : Date.now();
    var decisions = [], updates = [], finished = [];
    (departments || []).forEach(function (d) {
      d.staff.forEach(function (x) {
        if (x.standing === 'decision') decisions.push(x);
        else if (x.standing === 'update') updates.push(x);
        if (finishedToday(x.s, now)) finished.push(x);
      });
    });
    decisions.sort(function (a, b) { return (a.s.since || a.s.lastAt || 0) - (b.s.since || b.s.lastAt || 0); });
    var newest = function (a, b) { return (b.s.summary && b.s.summary.turnAt || b.s.lastAt || 0) - (a.s.summary && a.s.summary.turnAt || a.s.lastAt || 0); };
    updates.sort(newest);
    finished.sort(newest);
    return { decisions: decisions, updates: updates, finished: finished };
  }

  // The Chief of Staff's one-line briefing.
  function briefing(departments, lists) {
    var waiting = (departments || []).filter(function (d) { return d.inOval; }).length;
    var working = (departments || []).reduce(function (n, d) { return n + d.working; }, 0);
    var dec = lists.decisions.length, upd = lists.updates.length;
    var head = dec
      ? 'Mr. President, ' + dec + ' decision' + (dec === 1 ? ' is' : 's are') + ' waiting, brought by ' + waiting + ' secretar' + (waiting === 1 ? 'y' : 'ies') + '.'
      : 'Mr. President, nothing needs a decision.';
    return head + ' ' + upd + ' update' + (upd === 1 ? ' is' : 's are') + ' for your information, and ' + working + ' staff ' + (working === 1 ? 'is' : 'are') + ' at work.';
  }

  // Where each office sits, in tiles. The Oval Office is fixed in the middle
  // of the top row with two offices either side; more offices fill rows of six
  // below the corridor. Returns offices in department order plus the fixed rooms.
  function layout(count) {
    var OFF_W = 7, OFF_H = 12, OVAL_W = 16, CORR_H = 3, SIDE = 2;
    var width = SIDE * 2 * OFF_W + OVAL_W;               // 44 tiles
    var offices = [];
    var top = Math.min(count, SIDE * 2);
    for (var i = 0; i < top; i++) {
      var slot = i < SIDE ? i : i + 0;                  // 0,1 left; 2,3 right
      var x = i < SIDE ? i * OFF_W : SIDE * OFF_W + OVAL_W + (i - SIDE) * OFF_W;
      offices.push({ x: x, y: 0, w: OFF_W, h: OFF_H, door: 'bottom', row: 0, slot: slot });
    }
    var rest = Math.max(0, Math.min(count, OFFICES_MAX) - top);
    var perRow = Math.floor(width / OFF_W);              // 6
    var rows = Math.ceil(rest / perRow);
    for (var j = 0; j < rest; j++) {
      var r = Math.floor(j / perRow), c = j % perRow;
      var inRow = Math.min(perRow, rest - r * perRow);
      var pad = Math.floor((width - inRow * OFF_W) / 2);
      offices.push({ x: pad + c * OFF_W, y: OFF_H + CORR_H + r * OFF_H, w: OFF_W, h: OFF_H, door: 'top', row: 1 + r, slot: c });
    }
    return {
      tile: 16, width: width, height: OFF_H + CORR_H + rows * OFF_H,
      oval: { x: SIDE * OFF_W, y: 0, w: OVAL_W, h: OFF_H },
      corridor: { x: 0, y: OFF_H, w: width, h: CORR_H },
      offices: offices
    };
  }

  return {
    standing: standing, need: need, finishedToday: finishedToday,
    buildDepartments: buildDepartments, drawnCount: function (deps) { return (deps || []).filter(function (d) { return d.drawn; }).length; }, deskLists: deskLists, briefing: briefing, layout: layout,
    OFFICES_MAX: OFFICES_MAX, DESKS_PER_OFFICE: DESKS_PER_OFFICE
  };
}));
