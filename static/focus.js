/*
 * Agent Command Center: the model behind the focus view.
 *
 * The focus view shows one session in depth, following the one you have in
 * front. This file decides which session that is and works out what the page
 * says about it: where the current turn's time went, the day's totals, and a
 * timeline with repeated lines folded. It builds on the session records that
 * sessions.js derives from the event stream.
 *
 * Pure functions, no DOM: the page renders the result, and tests run this file
 * under Node (see tests/test_focus_js.py). Works in browsers as window.ACCFocus
 * and in Node via module.exports.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./sessions.js'));
  else root.ACCFocus = factory(root.ACCSessions);
}(typeof self !== 'undefined' ? self : this, function (S) {
  'use strict';

  var MIN = 60 * 1000;

  // The prompt-submit event that opens a turn (the same test sessions.js uses).
  function isTurnStart(e) {
    return e.status === 'started' && e.action === 'Working' && (!e.doing || e.doing === 'Thinking') &&
      typeof e.steps_total !== 'number';
  }

  // What closes a turn. "Session resumed" is not one: the hook sends it when the
  // session's context is compacted mid-turn, and when a message resumes a
  // session (then it follows the prompt that opened the turn).
  function isTurnEnd(e) {
    return e.status === 'finished' || e.action === 'Session closed' || e.action === 'Session opened';
  }

  function tsOf(e) {
    var t = Date.parse(e && e.ts);
    return isNaN(t) ? null : t;
  }

  // Oldest first; a session record keeps its events newest first.
  function chronological(s) {
    return (s && s.events ? s.events : []).slice().reverse();
  }

  // When you last sent this session a message, or null.
  function lastPromptAt(s) {
    var list = s && s.events ? s.events : [];
    for (var i = 0; i < list.length; i++) {
      if (isTurnStart(list[i])) return tsOf(list[i]);
    }
    return null;
  }

  // Which session the page shows.
  //
  // A pinned session wins while it is still listed. Otherwise the page follows
  // you: for each session, the newer of when the desktop app last showed it
  // (links[session].focused_at, from /api/links) and when you last sent it a
  // message. The newest wins; with nothing to go on, the most urgent session
  // (the first in the derived order) does. Closed sessions are never chosen.
  function pickFocus(sessions, links, pinnedKey) {
    var list = (sessions || []).filter(function (s) { return s.state !== 'closed'; });
    if (!list.length) return null;
    if (pinnedKey) {
      for (var p = 0; p < list.length; p++) if (list[p].key === pinnedKey) return pinnedKey;
    }
    var best = null, bestAt = -Infinity;
    list.forEach(function (s) {
      var entry = links && s.session && links[s.session];
      var focused = entry && typeof entry.focused_at === 'number' ? entry.focused_at : null;
      var prompted = lastPromptAt(s);
      var at = Math.max(focused === null ? -Infinity : focused, prompted === null ? -Infinity : prompted);
      if (at > bestAt) { bestAt = at; best = s; }
    });
    return (best || list[0]).key;
  }

  // The turn the page describes: the live one, else the last finished one.
  // {start, end (null while live), live, spans: [{label, from, to}]} or null.
  function currentTurn(s, now) {
    var list = chronological(s);
    var start = -1;
    for (var i = list.length - 1; i >= 0; i--) {
      if (isTurnStart(list[i])) { start = i; break; }
    }
    if (start < 0) return null;
    var startAt = tsOf(list[start]);
    var end = null;
    var spans = [];
    var label = 'Thinking', from = startAt, before = label;  // before: the activity an ask interrupted
    for (var j = start + 1; j < list.length; j++) {
      var e = list[j], t = tsOf(e);
      if (t === null) continue;
      if (isTurnEnd(e)) { end = t; break; }
      var next = null;
      if (e.status === 'awaiting_approval') { if (label !== 'Waiting for you') before = label; next = 'Waiting for you'; }
      else if (e.status === 'started' && e.doing && typeof e.steps_total !== 'number') next = e.doing;
      else if (e.status === 'approved' || e.status === 'declined') next = before;
      if (next && next !== label) {
        if (t > from) spans.push({ label: label, from: from, to: t });
        label = next; from = t;
      }
    }
    var to = end !== null ? end : now;
    if (to > from) spans.push({ label: label, from: from, to: to });
    return { start: startAt, end: end, live: end === null, spans: spans };
  }

  // Where the turn's time went, largest first: [{label, ms, share}].
  function timeMix(turn) {
    if (!turn) return [];
    var byLabel = {};
    turn.spans.forEach(function (sp) { byLabel[sp.label] = (byLabel[sp.label] || 0) + (sp.to - sp.from); });
    var total = 0;
    Object.keys(byLabel).forEach(function (k) { total += byLabel[k]; });
    return Object.keys(byLabel).map(function (k) {
      return { label: k, ms: byLabel[k], share: total ? byLabel[k] / total : 0 };
    }).sort(function (a, b) { return b.ms - a.ms; });
  }

  // Since dayStart (ms): how many turns you started, how long the session
  // worked, and how long it sat waiting on you (open asks, plus the gaps where
  // it had finished a turn and waited for your next message are NOT counted:
  // that is your time, not its).
  function dayStats(s, now, dayStart) {
    var list = chronological(s);
    var turns = 0, worked = 0, waited = 0;
    var turnAt = null, askAt = null;
    list.forEach(function (e) {
      var t = tsOf(e);
      if (t === null || t > now) return;
      if (isTurnStart(e)) {
        if (turnAt !== null) worked += Math.max(0, Math.min(t, now) - Math.max(turnAt, dayStart));
        turnAt = t;
        if (t >= dayStart) turns += 1;
      }
      if (e.status === 'awaiting_approval' && askAt === null) askAt = t;
      if ((e.status === 'approved' || e.status === 'declined') && askAt !== null) {
        waited += Math.max(0, t - Math.max(askAt, dayStart));
        askAt = null;
      }
      if (isTurnEnd(e)) {
        if (turnAt !== null) worked += Math.max(0, t - Math.max(turnAt, dayStart));
        turnAt = null;
        if (askAt !== null) { waited += Math.max(0, t - Math.max(askAt, dayStart)); askAt = null; }
      }
    });
    if (turnAt !== null) worked += Math.max(0, now - Math.max(turnAt, dayStart));
    if (askAt !== null) waited += Math.max(0, now - Math.max(askAt, dayStart));
    return { turns: turns, workedMs: worked, waitedMs: waited };
  }

  // The timeline, newest first, with runs of the same line folded into one
  // entry that carries a count and the run's first and last time.
  function foldTimeline(s, limit) {
    var out = [];
    var list = s && s.events ? s.events : [];
    for (var i = 0; i < list.length; i++) {
      var e = list[i], t = tsOf(e);
      if (t === null) continue;
      var line = S.eventLine(e);
      var kind = e.status === 'awaiting_approval' ? 'ask' : e.status === 'failed' ? 'fail'
        : isTurnStart(e) ? 'prompt' : e.status === 'finished' ? 'turn'
        : (e.status === 'started' || e.status === 'approved') ? 'work' : 'quiet';
      var last = out[out.length - 1];
      if (last && last.line === line && last.kind === kind) {
        last.count += 1;
        last.from = t;  // older: the run began earlier
      } else {
        if (limit && out.length >= limit) break;
        out.push({ line: line, kind: kind, count: 1, at: t, from: t });
      }
    }
    return out;
  }

  // Other sessions, most urgent first, with the focused one left out.
  function others(sessions, focusKey) {
    return (sessions || []).filter(function (s) { return s.key !== focusKey && s.state !== 'closed'; });
  }

  // Blocked or failed sessions other than the focused one: the alert strip.
  function blockedElsewhere(sessions, focusKey) {
    return others(sessions, focusKey).filter(function (s) { return s.state === 'need' || s.state === 'error'; });
  }

  // Whole seconds for a live clock: "4m 12s", "1h 03m 09s".
  function clock(ms) {
    if (ms === null || ms === undefined || ms < 0) return '';
    var sec = Math.floor(ms / 1000);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
    var two = function (n) { return (n < 10 ? '0' : '') + n; };
    if (h) return h + 'h ' + two(m) + 'm ' + two(x) + 's';
    return m + 'm ' + two(x) + 's';
  }

  return {
    pickFocus: pickFocus, lastPromptAt: lastPromptAt, currentTurn: currentTurn, timeMix: timeMix,
    dayStats: dayStats, foldTimeline: foldTimeline, others: others, blockedElsewhere: blockedElsewhere,
    clock: clock, MIN: MIN
  };
}));
