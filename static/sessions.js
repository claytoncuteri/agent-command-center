/*
 * Agent Command Center: session model for the session-first view.
 *
 * Turns the raw acc/1 event stream into one record per session with a plain
 * state (needs you, working, quiet, your turn, idle, closed, error), what it is
 * doing now, how long it has been in that state, and a small activity bar.
 *
 * Pure functions, no DOM: the page renders the result, and tests run this file
 * under Node (see tests/test_sessions_js.py). Works in browsers as
 * window.ACCSessions and in Node via module.exports.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ACCSessions = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MIN = 60 * 1000;
  var HOUR = 60 * MIN;
  var QUIET_AFTER = 10 * MIN;     // working but silent this long: may be stuck
  var IDLE_AFTER = 6 * HOUR;      // "your turn" older than this counts as idle
  var CLOSED_WINDOW = 24 * HOUR;  // closed sessions older than this are dropped
  var BAR_SLOTS = 12;             // activity bar: 12 slots of 5 minutes
  var BAR_SLOT = 5 * MIN;

  var RESOLVED = { approved: true, declined: true };

  function tsOf(e) {
    var t = Date.parse(e && e.ts);
    return isNaN(t) ? null : t;
  }

  function keyOf(e) {
    return (e.host || '') + '|' + (e.session || e.agent || 'unknown');
  }

  // "permission: Bash" -> what Claude wants to do, in plain words.
  var TOOL_ASK = {
    Bash: 'run a shell command (Bash)',
    Edit: 'edit files', MultiEdit: 'edit files', Write: 'write files', NotebookEdit: 'edit a notebook',
    Read: 'read files', WebFetch: 'fetch a web page', WebSearch: 'search the web',
    Task: 'start a sub-agent', Agent: 'start a sub-agent',
    Artifact: 'publish a page', SendMessage: 'message another session'
  };
  // The desktop app's own tools, by tool name.
  var APP_ASK = {
    start_session: 'start a new session', set_remote_control: 'change Remote Control',
    set_session_title: 'rename a session', archive_session: 'archive a session',
    set_setting: 'change an app setting', move_sessions: 'move sessions in the sidebar'
  };

  function toolAsk(tool) {
    if (!tool) return 'continue';
    if (TOOL_ASK[tool]) return TOOL_ASK[tool];
    if (tool.indexOf('mcp__') === 0) {
      var parts = tool.split('__');
      var server = (parts[1] || '').toLowerCase();
      if (server.indexOf('browser') >= 0 || server.indexOf('chrome') >= 0) return 'use the browser';
      if (APP_ASK[parts[2]]) return APP_ASK[parts[2]];
      if (server.indexOf('ccd_') === 0) return 'use the app (' + (parts[2] || '').replace(/_/g, ' ') + ')';
      return 'use a connector tool' + (parts[2] ? ' (' + parts[2].replace(/_/g, ' ') + ')' : '');
    }
    return 'use ' + tool;
  }

  function askOf(e) {
    var detail = e.detail || '';
    if (e.source === 'claude-code' || /^permission|^question$/.test(detail)) {
      if (detail === 'question') return { kind: 'Question', text: 'Claude is asking you to choose' };
      if (detail.indexOf('permission:') === 0) {
        return { kind: 'Permission', text: toolAsk(detail.slice('permission:'.length).trim()) };
      }
      if (detail === 'permission') return { kind: 'Permission', text: 'continue' };
    }
    return { kind: 'Approval', text: (e.action || 'Waiting') + (detail ? ' · ' + detail : '') };
  }

  function isTurnStart(e) {
    return e.status === 'started' && e.action === 'Working' && (!e.doing || e.doing === 'Thinking');
  }

  // opts.bootedAt (ms): when the machine running the sessions last started, if known.
  function deriveSessions(events, now, opts) {
    now = typeof now === 'number' ? now : Date.now();
    var bootedAt = opts && typeof opts.bootedAt === 'number' ? opts.bootedAt : null;
    var groups = {};
    (events || []).forEach(function (e) {
      if (!e || !e.agent) return;
      var t = tsOf(e);
      if (t === null) return;
      var k = keyOf(e);
      (groups[k] = groups[k] || []).push({ e: e, t: t });
    });

    var sessions = [];
    Object.keys(groups).forEach(function (k) {
      var list = groups[k].sort(function (a, b) { return a.t - b.t; });
      var s = {
        key: k, project: null, title: null, host: null, session: null, source: null,
        state: null, since: null, ask: null, doing: null, note: null,
        turnStartedAt: null, lastTurnMs: null, lastAt: null, openedAt: null,
        lastAction: null, lastStatus: null, bars: [], events: []
      };
      var open = {};      // approval_id -> {e, t}
      var lastState = null;

      list.forEach(function (it) {
        var e = it.e, t = it.t;
        s.project = e.agent;
        s.host = e.host || s.host;
        s.session = e.session || s.session;
        s.source = e.source || s.source;
        if (e.title) s.title = e.title;
        s.lastAt = t;
        if (e.action === 'Session opened' && s.openedAt === null) s.openedAt = t;

        // A Claude Code ask cannot outlive its turn: the hook closes any open ask
        // when a turn ends or a new one starts. So a turn boundary clears asks a
        // missed close left behind. (Not for other agent stacks, where an
        // approval can stay open across unrelated tasks.)
        if (e.source === 'claude-code' && (e.status === 'finished' || isTurnStart(e) ||
            e.action === 'Session opened' || e.action === 'Session resumed' || e.action === 'Session closed')) {
          open = {};
        }
        if (e.status === 'awaiting_approval' && e.approval_id) open[e.approval_id] = it;
        if (RESOLVED[e.status] && e.approval_id) delete open[e.approval_id];

        if (isTurnStart(e)) { s.turnStartedAt = t; s.doing = null; }
        if (e.doing && e.doing !== 'Thinking') s.doing = e.doing;
        else if (e.doing === 'Thinking' && isTurnStart(e)) s.doing = 'Thinking';
        if (e.status === 'finished') {
          s.lastTurnMs = s.turnStartedAt !== null ? t - s.turnStartedAt : null;
          s.turnStartedAt = null;
          s.doing = null;
        }
        if (e.action === 'Session opened' || e.action === 'Session resumed') {
          s.turnStartedAt = null;
          s.doing = null;
        }
        lastState = it;
        s.lastAction = e.action || null;
        s.lastStatus = e.status || null;
      });

      var openList = Object.keys(open).map(function (id) { return open[id]; })
        .sort(function (a, b) { return a.t - b.t; });
      var last = lastState.e, lastT = lastState.t;

      if (openList.length) {
        s.state = 'need';
        s.since = openList[0].t;
        s.ask = askOf(openList[0].e);
        s.openCount = openList.length;
      } else if (last.status === 'failed') {
        s.state = 'error';
        s.since = lastT;
        s.note = last.detail || last.result || 'The last task failed';
      } else if (last.status === 'started' || RESOLVED[last.status]) {
        s.state = (now - lastT > QUIET_AFTER) ? 'quiet' : 'work';
        s.since = s.turnStartedAt !== null ? s.turnStartedAt : lastT;
      } else if (last.status === 'idle' && last.action === 'Session closed') {
        s.state = 'closed';
        s.since = lastT;
      } else if (last.status === 'finished') {
        s.state = (now - lastT > IDLE_AFTER) ? 'idle' : 'turn';
        s.since = lastT;
      } else {
        // Opened or resumed (or a generic info/idle event): waiting for a message.
        s.state = (now - lastT > IDLE_AFTER) ? 'idle' : 'turn';
        s.since = lastT;
        s.fresh = last.action === 'Session opened' || last.action === 'Session resumed';
      }

      // A session that was blocked or working when the machine last restarted
      // cannot still be: the restart cut it off. Without this it would sit in
      // Blocked or Working (red, "may be stuck") until someone reopened it.
      if (bootedAt && lastT < bootedAt && (s.state === 'need' || s.state === 'work' || s.state === 'quiet')) {
        s.interruptedFrom = s.state;
        s.state = 'idle';
        s.interrupted = true;
        s.ask = null;
        s.since = lastT;
      }

      // Activity bar over the last hour, oldest slot first.
      var start = now - BAR_SLOTS * BAR_SLOT;
      for (var i = 0; i < BAR_SLOTS; i++) s.bars.push('');
      list.forEach(function (it) {
        if (it.t < start || it.t > now) return;
        var slot = Math.min(BAR_SLOTS - 1, Math.floor((it.t - start) / BAR_SLOT));
        var mark = it.e.status === 'awaiting_approval' ? 'a'
          : it.e.status === 'finished' ? 't'
          : (it.e.status === 'started' || RESOLVED[it.e.status]) ? 'w' : '';
        var rank = { '': 0, t: 1, w: 2, a: 3 };
        if (rank[mark] > rank[s.bars[slot]]) s.bars[slot] = mark;
      });

      s.events = list.slice().reverse().map(function (it) { return it.e; });
      if (s.state === 'closed' && now - s.since > CLOSED_WINDOW) return;
      sessions.push(s);
    });

    var order = { need: 0, error: 0, work: 1, quiet: 1, turn: 2, idle: 3, closed: 4 };
    sessions.sort(function (a, b) {
      if (order[a.state] !== order[b.state]) return order[a.state] - order[b.state];
      if (a.state === 'need' || a.state === 'error') return a.since - b.since;   // oldest ask first
      if (a.state !== b.state) return a.state === 'quiet' ? 1 : -1;              // quiet after working
      return b.lastAt - a.lastAt;                                                 // most recent first
    });

    var counts = { need: 0, work: 0, turn: 0, idle: 0, closed: 0 };
    var projects = {};
    sessions.forEach(function (s) {
      var bucket = (s.state === 'error') ? 'need' : (s.state === 'quiet') ? 'work' : s.state;
      counts[bucket] += 1;
      var p = projects[s.project] = projects[s.project] || { name: s.project, total: 0, need: 0 };
      if (s.state !== 'closed') p.total += 1;
      if (bucket === 'need') p.need += 1;
    });
    var projectList = Object.keys(projects).map(function (n) { return projects[n]; })
      .filter(function (p) { return p.total > 0 || p.need > 0; })
      .sort(function (a, b) { return (b.need - a.need) || (b.total - a.total) || (a.name < b.name ? -1 : 1); });
    var hosts = {};
    sessions.forEach(function (s) { if (s.host && s.state !== 'closed') hosts[s.host] = true; });

    return { sessions: sessions, counts: counts, projects: projectList, hosts: Object.keys(hosts) };
  }

  function rel(ms) {
    if (ms < 45 * 1000) return 'now';
    if (ms < HOUR) return Math.max(1, Math.round(ms / MIN)) + 'm';
    if (ms < 24 * HOUR) return Math.floor(ms / HOUR) + 'h';
    return Math.floor(ms / (24 * HOUR)) + 'd';
  }

  function dur(ms) {
    if (ms === null || ms === undefined || ms < 0) return '';
    if (ms < MIN) return Math.max(1, Math.round(ms / 1000)) + 's';
    if (ms < HOUR) return Math.round(ms / MIN) + 'm';
    var h = Math.floor(ms / HOUR), m = Math.round((ms % HOUR) / MIN);
    return h + 'h' + (m ? ' ' + m + 'm' : '');
  }

  // The one-line "what is going on" for a row.
  function describe(s, now) {
    now = typeof now === 'number' ? now : Date.now();
    switch (s.state) {
      case 'need': return s.ask ? s.ask.kind + ': ' + s.ask.text : 'Waiting for you';
      case 'error': return 'Error: ' + (s.note || 'failed');
      case 'work':
        return (s.doing || 'Working') + (s.turnStartedAt !== null ? ' · turn running ' + dur(now - s.turnStartedAt) : '');
      case 'quiet': return (s.doing || 'Working') + ' · silent ' + dur(now - s.lastAt);
      case 'turn':
        if (s.fresh) return 'Opened, waiting for your first message';
        return s.lastTurnMs !== null ? 'Finished a turn after ' + dur(s.lastTurnMs) : 'Finished a turn';
      case 'idle':
        if (s.interrupted) {
          return (s.interruptedFrom === 'need' ? 'Was waiting on you' : 'Was working') +
            ' when the computer restarted · open it to carry on';
        }
        return 'Last: ' + (s.lastAction || 'activity').toLowerCase();
      case 'closed': return 'Closed';
    }
    return '';
  }

  // Plain-language line for one event in the session timeline.
  function eventLine(e) {
    if (e.status === 'awaiting_approval') {
      var a = askOf(e);
      return 'Asked ' + (a.kind === 'Question' ? 'you a question' : 'permission to ' + a.text);
    }
    if (e.status === 'started' && e.action === 'Working') {
      if (!e.doing || e.doing === 'Thinking') return 'You sent a message; working';
      return e.doing;
    }
    if (e.status === 'finished' && e.action === 'Your turn') return 'Finished a turn';
    return (e.action || e.status || '') + (e.result && e.result !== 'not run' ? ' (' + e.result + ')' : '');
  }

  return {
    deriveSessions: deriveSessions, describe: describe, eventLine: eventLine,
    rel: rel, dur: dur, toolAsk: toolAsk,
    QUIET_AFTER: QUIET_AFTER, IDLE_AFTER: IDLE_AFTER
  };
}));
