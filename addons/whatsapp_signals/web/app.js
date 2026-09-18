/* WhatsApp Signals - operator page.
 *
 * Plain JavaScript on purpose: this add-on stays out of frontend/, so there is
 * no bundler here and nothing to rebuild when upstream ships a new UI.
 *
 * Every POST carries the platform's CSRF token, fetched the same way the React
 * app fetches it. The token is cached for the life of the page and refetched
 * once if a request is rejected, which is what happens after a long idle.
 */

(function () {
  'use strict';

  var REFRESH_MS = 6000;
  var csrfToken = null;
  var state = null;
  var expanded = {};      // chat_jid -> whether its settings are open
  var dirty = {};         // chat_jid -> unsaved edits, so a refresh cannot wipe typing

  // ---------------------------------------------------------------- transport

  function getCsrf(force) {
    if (csrfToken && !force) return Promise.resolve(csrfToken);
    return fetch('/auth/csrf-token', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { csrfToken = d.csrf_token || null; return csrfToken; });
  }

  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (r.status === 401 || r.status === 403 || r.redirected) {
        throw new Error('Your session has ended. Sign in to OpenAlgo again.');
      }
      return r.json();
    });
  }

  function postJSON(url, body, retried) {
    return getCsrf(!!retried).then(function (token) {
      return fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token || '' },
        body: JSON.stringify(body || {})
      }).then(function (r) {
        if (r.status === 400 && !retried) return postJSON(url, body, true);
        return r.json().catch(function () {
          return { status: 'error', message: 'The server did not answer that request.' };
        });
      });
    });
  }

  // ------------------------------------------------------------------ helpers

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else if (k === 'html') node.innerHTML = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function money(v) {
    if (v === null || v === undefined || v === '') return '-';
    return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }

  function when(iso) {
    if (!iso) return '-';
    var d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    if (isNaN(d.getTime())) return '-';
    return d.toLocaleString('en-IN', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }

  // -------------------------------------------------------------------- views

  function renderStatus() {
    var strip = document.getElementById('status-strip');
    clear(strip);
    if (!state) return;
    var bot = state.bot || {};
    var worker = state.worker || {};

    function pill(label, value, tone) {
      return el('span', { class: 'pill ' + (tone || '') }, [
        document.createTextNode(label + ' '), el('b', { text: value })
      ]);
    }

    strip.appendChild(pill('Device', bot.is_ready ? 'linked' : (bot.is_paired ? 'paired, offline' : 'not linked'),
      bot.is_ready ? 'ok' : 'bad'));
    strip.appendChild(pill('Reader', worker.is_running ? 'running' : 'stopped',
      worker.is_running ? 'ok' : 'bad'));
    strip.appendChild(pill('Platform', state.platform_mode === 'live' ? 'live' : 'sandbox',
      state.platform_mode === 'live' ? 'warn' : 'ok'));
    strip.appendChild(pill('Model', state.llm_available ? 'available' : 'not set up', ''));
  }

  function renderBanners() {
    var slot = document.getElementById('banner-slot');
    clear(slot);
    if (!state) return;
    var notes = [];

    if (!state.bot.is_paired) {
      notes.push(['bad', 'No WhatsApp device is linked, so no group messages are arriving. ' +
        'Link your phone on the WhatsApp Bot page, then come back here.']);
    } else if (!state.bot.is_ready) {
      notes.push(['bad', 'The linked device is not connected right now. Start the bot on the ' +
        'WhatsApp Bot page - signals are not being read until it is.']);
    }
    if (!state.worker.is_running) {
      notes.push(['bad', 'The signal reader is not running. Restart OpenAlgo; if it stays ' +
        'stopped, the log will say why.']);
    }

    var liveGroups = (state.groups || []).filter(function (g) {
      return g.is_enabled && g.execution_mode === 'live';
    });
    if (liveGroups.length && state.platform_mode === 'live') {
      notes.push(['warn', 'Live trading is on. Signals from ' + liveGroups.length +
        ' enabled group' + (liveGroups.length > 1 ? 's' : '') +
        ' will place real orders with real money.']);
    }
    var sandboxLive = (state.groups || []).filter(function (g) {
      return g.is_enabled && g.execution_mode === 'analyze';
    });
    if (sandboxLive.length && state.platform_mode === 'live') {
      notes.push(['warn', 'Some enabled groups are set to sandbox while the platform is live. ' +
        'Their signals are refused rather than sent, so nothing from them is being traded.']);
    }

    notes.forEach(function (n) {
      slot.appendChild(el('p', { class: 'notice ' + (n[0] === 'bad' ? 'bad' : ''), text: n[1] }));
    });
  }

  function groupValue(jid, key, fallback) {
    if (dirty[jid] && Object.prototype.hasOwnProperty.call(dirty[jid], key)) return dirty[jid][key];
    return fallback;
  }

  function field(jid, key, label, type, value, options) {
    var input;
    if (type === 'select') {
      input = el('select', {});
      options.forEach(function (opt) {
        var o = el('option', { value: opt[0], text: opt[1] });
        if (String(opt[0]) === String(value)) o.selected = true;
        input.appendChild(o);
      });
    } else {
      input = el('input', { type: type, value: value === null || value === undefined ? '' : value });
      if (type === 'number') { input.setAttribute('min', '0'); input.setAttribute('step', 'any'); }
    }
    input.addEventListener('change', function () {
      dirty[jid] = dirty[jid] || {};
      dirty[jid][key] = type === 'number'
        ? (input.value === '' ? null : Number(input.value))
        : input.value;
    });
    return el('div', { class: 'field' }, [el('label', { text: label }), input]);
  }

  function checkbox(jid, key, label, checked) {
    var input = el('input', { type: 'checkbox' });
    input.checked = !!checked;
    input.addEventListener('change', function () {
      dirty[jid] = dirty[jid] || {};
      dirty[jid][key] = input.checked;
    });
    return el('label', { class: 'check' }, [input, document.createTextNode(' ' + label)]);
  }

  function renderGroups() {
    var host = document.getElementById('groups');
    clear(host);
    var groups = (state && state.groups) || [];
    if (!groups.length) {
      host.appendChild(el('p', { class: 'empty', text:
        'No groups seen yet. Once the linked device receives a message in a group, ' +
        'that group appears here.' }));
      return;
    }

    groups.forEach(function (g) {
      var jid = g.chat_jid;
      var open = !!expanded[jid];

      var toggle = el('button', {
        class: 'btn ' + (g.is_enabled ? '' : 'ghost'),
        text: g.is_enabled ? 'Enabled' : 'Disabled'
      });
      toggle.addEventListener('click', function () {
        save(jid, { is_enabled: !g.is_enabled });
      });

      var settingsBtn = el('button', { class: 'btn ghost', text: open ? 'Hide settings' : 'Settings' });
      settingsBtn.addEventListener('click', function () {
        expanded[jid] = !open;
        renderGroups();
      });

      var head = el('div', { class: 'group-head' }, [
        el('span', { class: 'group-name', text: g.label || jid.split('@')[0] }),
        el('span', { class: 'group-jid', text: jid }),
        el('span', { class: 'group-meta', text:
          g.message_count + ' messages seen - last ' + when(g.last_seen_at) }),
        toggle, settingsBtn
      ]);

      var block = el('div', { class: 'group' }, [head]);

      if (open) {
        var grid = el('div', { class: 'grid' }, [
          field(jid, 'label', 'Name', 'text', groupValue(jid, 'label', g.label || '')),
          field(jid, 'execution_mode', 'Trade in', 'select',
            groupValue(jid, 'execution_mode', g.execution_mode),
            [['analyze', 'Sandbox'], ['live', 'Live money']]),
          field(jid, 'product', 'Product', 'select',
            groupValue(jid, 'product', g.product),
            [['MIS', 'MIS (intraday)'], ['NRML', 'NRML (carry)']]),
          field(jid, 'lots', 'Lots per signal', 'number', groupValue(jid, 'lots', g.lots)),
          field(jid, 'max_lots', 'Never more than', 'number', groupValue(jid, 'max_lots', g.max_lots)),
          field(jid, 'max_open_positions', 'Open positions cap', 'number',
            groupValue(jid, 'max_open_positions', g.max_open_positions)),
          field(jid, 'max_signals_per_day', 'Signals per day cap', 'number',
            groupValue(jid, 'max_signals_per_day', g.max_signals_per_day)),
          field(jid, 'default_sl_pct', 'Default stop (% of entry)', 'number',
            groupValue(jid, 'default_sl_pct', g.default_sl_pct)),
          field(jid, 'default_target_pct', 'Default target (%)', 'number',
            groupValue(jid, 'default_target_pct', g.default_target_pct)),
          field(jid, 'trailing_step', 'Trailing step', 'number',
            groupValue(jid, 'trailing_step', g.trailing_step)),
          field(jid, 'allowed_senders_text', 'Only these senders (comma separated, blank = anyone)',
            'text', groupValue(jid, 'allowed_senders_text', (g.allowed_senders || []).join(', ')))
        ]);

        var checks = el('div', { class: 'actions' }, [
          checkbox(jid, 'trailing_enabled', 'Trail the stop', g.trailing_enabled),
          checkbox(jid, 'llm_fallback', 'Use the model for messages the rules cannot read', g.llm_fallback),
          checkbox(jid, 'notify_operator', 'Message me what was done', g.notify_operator)
        ]);

        var saveBtn = el('button', { class: 'btn', text: 'Save settings' });
        var status = el('span', {});
        saveBtn.addEventListener('click', function () {
          var changes = dirty[jid] || {};
          if (Object.prototype.hasOwnProperty.call(changes, 'allowed_senders_text')) {
            changes.allowed_senders = String(changes.allowed_senders_text || '')
              .split(',').map(function (s) { return s.trim(); })
              .filter(function (s) { return s.length; });
            delete changes.allowed_senders_text;
          }
          save(jid, changes, status);
        });

        var forget = el('button', { class: 'btn ghost', text: 'Forget this group' });
        forget.addEventListener('click', function () {
          if (!window.confirm('Forget ' + (g.label || jid) + '? Its settings and history stay ' +
            'in the log, but it will be treated as a new group if it posts again.')) return;
          postJSON('/whatsapp-signals/api/group/delete', { chat_jid: jid }).then(refresh);
        });

        block.appendChild(grid);
        block.appendChild(checks);
        block.appendChild(el('div', { class: 'actions' }, [saveBtn, forget, status]));
      }

      host.appendChild(block);
    });
  }

  function save(jid, changes, statusNode) {
    var body = Object.assign({ chat_jid: jid }, changes || {});
    postJSON('/whatsapp-signals/api/group', body).then(function (res) {
      if (res.status === 'success') {
        delete dirty[jid];
        if (statusNode) {
          statusNode.className = 'saved';
          statusNode.textContent = 'Saved';
        }
        refresh();
      } else if (statusNode) {
        statusNode.className = 'failed';
        statusNode.textContent = res.message || 'Could not save that.';
      }
    });
  }

  function renderPositions() {
    var host = document.getElementById('positions');
    clear(host);
    var rows = ((state && state.positions) || []).filter(function (p) { return p.status === 'open'; });
    if (!rows.length) {
      host.appendChild(el('p', { class: 'empty', text: 'No open positions from a signal.' }));
      return;
    }
    var table = el('table', {}, [
      el('thead', {}, [el('tr', {}, ['Instrument', 'Side', 'Qty', 'Entry', 'Stop', 'Target', 'Mode', 'Opened']
        .map(function (h) { return el('th', { text: h }); }))])
    ]);
    var body = el('tbody', {});
    rows.forEach(function (p) {
      body.appendChild(el('tr', {}, [
        el('td', { class: 'mono', text: p.symbol + ' - ' + p.exchange + ' ' + p.product }),
        el('td', { text: p.side }),
        el('td', { text: String(p.quantity) }),
        el('td', { text: money(p.entry_price) }),
        el('td', { text: money(p.stop_loss) }),
        el('td', { text: money(p.target) }),
        el('td', { text: p.mode === 'live' ? 'live' : 'sandbox' }),
        el('td', { text: when(p.opened_at) })
      ]));
    });
    table.appendChild(body);
    host.appendChild(table);
  }

  function renderEvents() {
    var host = document.getElementById('events');
    clear(host);
    var rows = (state && state.events) || [];
    if (!rows.length) {
      host.appendChild(el('p', { class: 'empty', text:
        'Nothing yet. Messages from enabled groups show up here as they arrive.' }));
      return;
    }
    var table = el('table', {}, [
      el('thead', {}, [el('tr', {}, ['Time', 'Message', 'Read as', 'Outcome', 'What happened']
        .map(function (h) { return el('th', { text: h }); }))])
    ]);
    var body = el('tbody', {});
    rows.forEach(function (e) {
      var readAs = e.action && e.action !== 'none'
        ? e.action.replace(/_/g, ' ') + (e.tier === 'llm' ? ' (model)' : '')
        : '-';
      body.appendChild(el('tr', {}, [
        el('td', { text: when(e.received_at) }),
        el('td', { class: 'msg', text: e.text || '' }),
        el('td', { text: readAs }),
        el('td', {}, [el('span', { class: 'tag ' + e.status, text: e.status })]),
        el('td', { class: 'msg', text: e.detail || '' })
      ]));
    });
    table.appendChild(body);
    host.appendChild(table);
  }

  function renderTestResult(res) {
    var host = document.getElementById('test-result');
    clear(host);
    if (!res) return;
    if (res.status !== 'success') {
      host.appendChild(el('p', { class: 'notice bad', text: res.message || 'That could not be read.' }));
      return;
    }
    var d = res.data, s = d.signal;
    var pairs = [
      ['Would trade', d.would_trade ? 'yes' : 'no'],
      ['Read as', s.action + (d.used_llm ? ' (model)' : ' (rules)')],
      ['Why', s.note || '-']
    ];
    if (s.side) pairs.push(['Side', s.side]);
    if (d.resolved) {
      pairs.push(['Instrument', d.resolved.symbol + ' on ' + d.resolved.exchange]);
      pairs.push(['Lot size', String(d.resolved.lotsize)]);
      if (d.resolved.expiry) pairs.push(['Expiry', d.resolved.expiry]);
    } else if (d.resolve_error) {
      pairs.push(['Instrument', d.resolve_error]);
    }
    if (s.entry_price) pairs.push(['Entry', money(s.entry_price)]);
    if (s.stop_loss) pairs.push(['Stop', money(s.stop_loss)]);
    if (s.sl_to_cost) pairs.push(['Stop', 'to cost']);
    if (s.target) pairs.push(['Target', money(s.target)]);
    if (s.fraction) pairs.push(['Closes', Math.round(s.fraction * 100) + '% of the position']);
    if (s.lots) pairs.push(['Lots', String(s.lots)]);

    var dl = el('dl', {});
    pairs.forEach(function (p) {
      dl.appendChild(el('dt', { text: p[0] }));
      dl.appendChild(el('dd', { text: String(p[1]) }));
    });
    host.appendChild(el('div', { class: 'result' }, [
      el('h3', { text: d.would_trade ? 'This would act' : 'This would be left alone' }), dl
    ]));
  }

  // ------------------------------------------------------------------ driving

  function render() {
    renderStatus();
    renderBanners();
    renderGroups();
    renderPositions();
    renderEvents();
  }

  function refresh() {
    return getJSON('/whatsapp-signals/api/state').then(function (res) {
      if (res.status !== 'success') return;
      state = res.data;
      render();
    }).catch(function (err) {
      var slot = document.getElementById('banner-slot');
      clear(slot);
      slot.appendChild(el('p', { class: 'notice bad', text: err.message }));
    });
  }

  document.getElementById('test-run').addEventListener('click', function () {
    var text = document.getElementById('test-text').value;
    if (!text.trim()) return;
    postJSON('/whatsapp-signals/api/parse', {
      text: text,
      use_llm: document.getElementById('test-llm').checked
    }).then(renderTestResult);
  });

  refresh();
  // Poll rather than subscribe: this page is open while someone watches it, and
  // a six-second poll of one bundled endpoint is cheaper to keep correct than a
  // socket channel that has to survive a reconnect.
  window.setInterval(function () {
    if (!document.hidden) refresh();
  }, REFRESH_MS);
})();
