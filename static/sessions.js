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

  // Friendly project names from the config's project_names, short to type:
  //   "Projects/Orion": "Orion"   a folder under home and everything inside it
  //   "orion-*": "Orion"          a folder name; * stands for the rest
  //   "src": "Orion"              a folder name exactly
  //   "Projects/Labs/lab-*": "*"  a * in the name repeats what the key's * matched,
  //                               made readable: lab-night-owl -> "Night Owl"
  // A path key's * matches within one folder. Case does not matter. The longest
  // path wins, then an exact folder name, then the longest pattern. A session
  // no rule matches keeps its folder name.
  function compileNames(map) {
    var rules = { paths: [], exact: {}, globs: [] };
    if (!map || typeof map !== 'object') return rules;
    Object.keys(map).forEach(function (k) {
      var name = map[k];
      if (typeof name !== 'string' || !name.trim()) return;
      name = name.trim().slice(0, 40);
      var key = k.trim().toLowerCase().replace(/^~\//, '').replace(/^\/+|\/+$/g, '');
      if (!key) return;
      var wild = key.slice(-1) === '*';
      if (wild) key = key.slice(0, -1);
      if (key.indexOf('/') >= 0) rules.paths.push({ key: key, wild: wild, name: name });
      else if (wild) rules.globs.push({ key: key, name: name });
      else rules.exact[key] = name;
    });
    function longestFirst(a, b) { return b.key.length - a.key.length; }
    rules.paths.sort(longestFirst);
    rules.globs.sort(longestFirst);
    return rules;
  }

  // "night_owl-app" -> "Night Owl App"
  function readable(text) {
    return text.replace(/[-_\s]+/g, ' ').trim().replace(/(^| )(\S)/g, function (m, sp, c) {
      return sp + c.toUpperCase();
    }).slice(0, 40);
  }

  // The rule's name, with any * replaced by what the key's * matched; null when
  // the name needs a match and there is none.
  function fill(name, rest) {
    if (name.indexOf('*') < 0) return name;
    var r = readable(rest || '');
    return r ? name.replace(/\*/g, r).slice(0, 40) : null;
  }

  // The friendly name for one event's folder, or null when no rule matches.
  function projectName(where, folder, rules) {
    var w = typeof where === 'string' ? where : '';
    var wl = w.toLowerCase();
    var i, n;
    for (i = 0; wl && i < rules.paths.length; i++) {
      var p = rules.paths[i];
      if (p.wild) {
        if (wl.indexOf(p.key) !== 0) continue;
        n = fill(p.name, w.slice(p.key.length).split('/')[0]);
        if (n) return n;
      } else if (wl === p.key || wl.indexOf(p.key + '/') === 0) {
        n = fill(p.name, '');
        if (n) return n;
      }
    }
    var f = folder || '';
    var fl = f.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(rules.exact, fl)) {
      n = fill(rules.exact[fl], '');
      if (n) return n;
    }
    for (i = 0; i < rules.globs.length; i++) {
      if (fl.indexOf(rules.globs[i].key) !== 0) continue;
      n = fill(rules.globs[i].name, f.slice(rules.globs[i].key.length));
      if (n) return n;
    }
    return null;
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

  // A new turn: the prompt-submit event ("Thinking"; older streams sent none).
  // A step-progress update is never one, even when it carries no activity.
  function isTurnStart(e) {
    return e.status === 'started' && e.action === 'Working' && (!e.doing || e.doing === 'Thinking') &&
      typeof e.steps_total !== 'number';
  }

  // opts.bootedAt (ms): when the machine running the sessions last started, if known.
  // opts.names: the config's project_names (see compileNames).
  // opts.summaries: /api/summaries, session id (8 chars) -> its latest turn summary.
  function deriveSessions(events, now, opts) {
    now = typeof now === 'number' ? now : Date.now();
    var bootedAt = opts && typeof opts.bootedAt === 'number' ? opts.bootedAt : null;
    var rules = compileNames(opts && opts.names);
    var summaries = opts && opts.summaries && typeof opts.summaries === 'object' && !Array.isArray(opts.summaries)
      ? opts.summaries : {};
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
        key: k, project: null, folder: null, title: null, host: null, session: null, source: null,
        state: null, since: null, ask: null, doing: null, note: null,
        turnStartedAt: null, lastTurnMs: null, lastAt: null, openedAt: null,
        lastAction: null, lastStatus: null, bars: [], events: [], steps: null,
        pauseRequested: false, turnFinished: false, limitMs: null, limitStart: null, limitResult: null,
        summary: null
      };
      var open = {};      // approval_id -> {e, t}
      var lastState = null;
      var named = null;   // the latest friendly name; a session may cd into an unnamed subfolder

      list.forEach(function (it) {
        var e = it.e, t = it.t;
        s.folder = e.agent;
        named = projectName(e.where, e.agent, rules) || named;
        s.project = named || e.agent;
        s.host = e.host || s.host;
        s.session = e.session || s.session;
        s.source = e.source || s.source;
        if (e.title) s.title = e.title;
        s.lastAt = t;
        if (e.action === 'Session opened' && s.openedAt === null) s.openedAt = t;
        // Step progress from the session's task list (counts only).
        if (typeof e.steps_total === 'number' && e.steps_total > 0) {
          s.steps = { done: Math.max(0, Math.min(e.steps_total, e.steps_done | 0)), total: e.steps_total,
                      current: typeof e.steps_current === 'number' ? e.steps_current : null };
        }
        if (e.action === 'Session opened') { s.steps = null; s.pauseRequested = false; s.turnFinished = false; }

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

        if (isTurnStart(e)) {
          s.turnStartedAt = t; s.doing = null; s.pauseRequested = e.pause === true; s.turnFinished = false;
          s.limitMs = typeof e.time_limit === 'number' ? e.time_limit * 1000 : null;
          s.limitStart = s.limitMs ? t : null; s.limitResult = null;
        }
        // A time limit sent mid-turn (queued message), spotted by the hook.
        if (e.action === 'Time limit set' && typeof e.time_limit === 'number') {
          s.limitMs = e.time_limit * 1000; s.limitStart = t;
        }
        // A pause request recorded after the fact (the one-time backfill).
        if (e.action === 'Pause requested' && e.pause === true) s.pauseRequested = true;
        if (e.doing && e.doing !== 'Thinking') s.doing = e.doing;
        else if (e.doing === 'Thinking' && isTurnStart(e)) s.doing = 'Thinking';
        if (e.status === 'finished') {
          if (e.pause === true) s.pauseRequested = true;
          s.turnFinished = true;
          s.lastTurnMs = s.turnStartedAt !== null ? t - s.turnStartedAt : null;
          s.turnStartedAt = null;
          s.doing = null;
        }
        if (e.action === 'Session opened' || e.action === 'Session resumed') {
          s.turnStartedAt = null;
          s.doing = null;
        }
        // Pause and time-limit markers do not change what the session is doing.
        if (e.action !== 'Pause requested' && e.action !== 'Time limit set') {
          lastState = it;
          s.lastAction = e.action || null;
          s.lastStatus = e.status || null;
        }
      });

      var openList = Object.keys(open).map(function (id) { return open[id]; })
        .sort(function (a, b) { return a.t - b.t; });
      if (!lastState) lastState = list[list.length - 1];  // only markers so far
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

      // A time limit: when the turn ends, finished work is Ready and unfinished
      // work is Paused. With a task list, its steps decide; without one, the clock.
      if (s.limitMs && s.turnFinished && (s.state === 'turn' || s.state === 'idle')) {
        var allDone = s.steps ? s.steps.done >= s.steps.total : null;
        var early = lastT - s.limitStart < s.limitMs - 60 * 1000;
        s.limitResult = (allDone === true || (allDone === null && early)) ? 'done' : 'paused';
        if (s.limitResult === 'paused') { s.pauseRequested = true; }
        else { s.fresh = false; }  // Ready (or Idle once old), never Paused
      }
      if (s.limitMs && !s.turnFinished) s.limitLeft = s.limitStart + s.limitMs - now;

      // You asked it to pause and it reached a pause point: it waits, however
      // long, until your next message. Working toward that point shows as working.
      if (s.pauseRequested && s.turnFinished && (s.state === 'turn' || s.state === 'idle')) {
        s.state = 'paused';
        s.fresh = false;
      }
      if (s.pauseRequested && !s.turnFinished && (s.state === 'work' || s.state === 'quiet')) s.pausing = true;

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

    var order = { need: 0, error: 0, work: 1, quiet: 1, turn: 2, paused: 3, idle: 4, closed: 5 };
    sessions.sort(function (a, b) {
      if (order[a.state] !== order[b.state]) return order[a.state] - order[b.state];
      if (a.state === 'need' || a.state === 'error') return a.since - b.since;   // oldest ask first
      if (a.state !== b.state) return a.state === 'quiet' ? 1 : -1;              // quiet after working
      return b.lastAt - a.lastAt;                                                 // most recent first
    });

    sessions.forEach(function (s) {
      if (s.session && Object.prototype.hasOwnProperty.call(summaries, s.session)) s.summary = cleanSummary(summaries[s.session]);
    });

    var counts = { need: 0, work: 0, turn: 0, paused: 0, idle: 0, closed: 0 };
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

  // "Step 3 of 6 · " while a task list is in progress; "" otherwise.
  function stepLabel(s) {
    if (!s.steps) return '';
    if (s.steps.done >= s.steps.total) return 'All ' + s.steps.total + ' steps done · ';
    return 'Step ' + (s.steps.current || s.steps.done + 1) + ' of ' + s.steps.total + ' · ';
  }

  // "Time limit · 6m left · " or "Over the time limit by 2m · " while working.
  function limitLabel(s) {
    if (!s.limitMs || typeof s.limitLeft !== 'number') return '';
    if (s.limitLeft >= 0) return 'Time limit \u00b7 ' + dur(Math.max(1000, s.limitLeft)) + ' left \u00b7 ';
    return 'Over the time limit by ' + dur(-s.limitLeft) + ' \u00b7 ';
  }

  // The one-line "what is going on" for a row.
  function describe(s, now) {
    now = typeof now === 'number' ? now : Date.now();
    switch (s.state) {
      case 'need': return s.ask ? s.ask.kind + ': ' + s.ask.text : 'Waiting for you';
      case 'error': return 'Error: ' + (s.note || 'failed');
      case 'work':
        return limitLabel(s) + (s.pausing ? 'Wrapping up to pause \u00b7 ' : '') + stepLabel(s) + (s.doing || 'Working') +
          (s.turnStartedAt !== null ? ' · turn running ' + dur(now - s.turnStartedAt) : '');
      case 'quiet': return limitLabel(s) + stepLabel(s) + (s.doing || 'Working') + ' · silent ' + dur(now - s.lastAt);
      case 'turn':
        if (s.fresh) return 'Opened, waiting for your first message';
        if (s.limitResult === 'done') return 'Finished within the ' + dur(s.limitMs) + ' time limit';
        return (s.lastTurnMs !== null ? 'Finished a turn after ' + dur(s.lastTurnMs) : 'Finished a turn') +
          (s.steps && s.steps.done < s.steps.total ? ' · ' + s.steps.done + ' of ' + s.steps.total + ' steps done' : '');
      case 'paused':
        return (s.limitResult === 'paused' ? 'Paused at the ' + dur(s.limitMs) + ' time limit' : 'Paused at your request') +
          (s.steps && s.steps.done < s.steps.total ? ' \u00b7 ' + s.steps.done + ' of ' + s.steps.total + ' steps done' : '') +
          ' \u00b7 send a message to resume';
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
    if (e.action === 'Pause requested') return 'You asked it to pause';
    if (e.action === 'Time limit set') return 'You set a ' + dur(e.time_limit * 1000) + ' time limit';
    if (e.status === 'started' && e.action === 'Working') {
      if (e.pause === true) return 'You asked it to pause; wrapping up';
      if (!e.doing || e.doing === 'Thinking') return 'You sent a message; working';
      return e.doing;
    }
    if (e.status === 'finished' && e.action === 'Your turn') return e.pause === true ? 'Stopped at a pause point' : 'Finished a turn';
    return (e.action || e.status || '') + (e.result && e.result !== 'not run' ? ' (' + e.result + ')' : '');
  }

  // A turn summary as the server sends it, re-checked: anything else is dropped.
  function cleanSummary(x) {
    if (!x || typeof x !== 'object' || Array.isArray(x)) return null;
    var str = function (v) { return typeof v === 'string' && v.trim() ? v : null; };
    var steps = Array.isArray(x.steps) ? x.steps.filter(function (st) { return st && typeof st === 'object'; })
      .map(function (st) {
        var ok = st.status === 'completed' || st.status === 'in_progress';
        return { name: str(st.name) || '', status: ok ? st.status : 'pending' };
      }) : [];
    return {
      steps: steps,
      stepsTotal: typeof x.steps_total === 'number' ? x.steps_total : steps.length,
      recap: str(x.recap), ask: str(x.ask),
      options: Array.isArray(x.options) ? x.options.filter(function (o) { return str(o); }) : [],
      blocked: x.blocked === true,
      turnAt: str(x.turn_at) ? Date.parse(x.turn_at) || null : null
    };
  }

  return {
    deriveSessions: deriveSessions, cleanSummary: cleanSummary, describe: describe, projectName: projectName, compileNames: compileNames, eventLine: eventLine,
    rel: rel, dur: dur, toolAsk: toolAsk, stepLabel: stepLabel, limitLabel: limitLabel,
    QUIET_AFTER: QUIET_AFTER, IDLE_AFTER: IDLE_AFTER
  };
}));
