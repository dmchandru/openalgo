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
  var activeChatJid = ''; // '' = all groups, or specific chat_jid
  var expanded = {};      // chat_jid -> whether its settings are open
  var dirty = {};         // chat_jid -> unsaved edits, so a refresh cannot wipe typing
  var profileFormOpen = false;
  var editingProfile = null;
  var groupFormOpen = false;

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

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

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

  function renderFilter() {
    var select = document.getElementById('group-filter');
    if (!select || !state) return;

    var current = select.value || activeChatJid;
    clear(select);

    var allOpt = el('option', { value: '', text: 'All groups / broadcasts' });
    if (!current) allOpt.selected = true;
    select.appendChild(allOpt);

    (state.groups || []).forEach(function (g) {
      var name = (g.label || g.chat_jid.split('@')[0]) + (g.is_enabled ? ' (enabled)' : ' (disabled)');
      var opt = el('option', { value: g.chat_jid, text: name });
      if (g.chat_jid === current) opt.selected = true;
      select.appendChild(opt);
    });

    select.onchange = function () {
      activeChatJid = select.value;
      renderGroups();
      renderPositions();
      renderEvents();
      renderSuggestions();
    };
  }

  function renderSuggestions() {
    var panel = document.getElementById('suggestions-panel');
    var host = document.getElementById('suggestions');
    if (!panel || !host) return;
    clear(host);

    var items = (state && state.pending_suggestions) || [];
    if (activeChatJid) {
      items = items.filter(function (s) { return s.chat_jid === activeChatJid; });
    }

    if (!items.length) {
      panel.style.display = 'none';
      return;
    }
    panel.style.display = '';

    items.forEach(function (s) {
      var grp = (state.groups || []).find(function (g) { return g.chat_jid === s.chat_jid; });
      var groupLabel = grp ? (grp.label || s.chat_jid.split('@')[0]) : s.chat_jid.split('@')[0];

      var head = el('div', { class: 'suggestion-head' }, [
        el('strong', { text: groupLabel }),
        el('span', { class: 'group-meta', text: when(s.created_at) })
      ]);

      var reason = el('div', { class: 'suggestion-reason', text: s.reasoning || 'No details provided.' });

      var actionsList = el('div', { class: 'actions-pills' }, (s.suggested_actions || []).map(function (a) {
        var txt = a.action.replace('_', ' ').toUpperCase();
        if (a.symbol) txt += ' ' + a.symbol;
        if (a.stop_loss) txt += ' SL: ' + money(a.stop_loss);
        if (a.sl_to_cost) txt += ' (SL to cost)';
        if (a.target) txt += ' TGT: ' + money(a.target);
        if (a.fraction) txt += ' (' + Math.round(a.fraction * 100) + '%)';
        return el('span', { class: 'action-pill', text: txt });
      }));

      var applyBtn = el('button', { class: 'btn', text: 'Apply' });
      var dismissBtn = el('button', { class: 'btn ghost', text: 'Dismiss' });
      var statusSpan = el('span', {});

      applyBtn.addEventListener('click', function () {
        applyBtn.disabled = true;
        dismissBtn.disabled = true;
        statusSpan.textContent = 'Applying...';
        postJSON('/whatsapp-signals/api/suggestion/apply', { id: s.id }).then(function (res) {
          if (res.status === 'success') {
            statusSpan.className = 'saved';
            statusSpan.textContent = 'Applied: ' + (res.data ? res.data.status : 'done');
            refresh();
          } else {
            statusSpan.className = 'failed';
            statusSpan.textContent = res.message || 'Failed to apply.';
            applyBtn.disabled = false;
            dismissBtn.disabled = false;
          }
        });
      });

      dismissBtn.addEventListener('click', function () {
        applyBtn.disabled = true;
        dismissBtn.disabled = true;
        postJSON('/whatsapp-signals/api/suggestion/dismiss', { id: s.id }).then(function () {
          refresh();
        });
      });

      var btns = el('div', { class: 'actions' }, [applyBtn, dismissBtn, statusSpan]);
      var card = el('div', { class: 'suggestion-card' }, [head, reason, actionsList, btns]);
      host.appendChild(card);
    });
  }

  function renderProfiles() {
    var host = document.getElementById('profiles-list');
    var formSlot = document.getElementById('profiles-form-slot');
    clear(host);
    clear(formSlot);
    if (!state) return;

    var profiles = state.profiles || [];

    // Form rendering if open
    if (profileFormOpen) {
      var p = editingProfile || {};
      var isEdit = !!p.id;

      var nameInput = el('input', { type: 'text', value: p.name || '', placeholder: 'e.g. Nifty Scalp Aggressive' });
      var orderTypeSelect = el('select', {}, [
        el('option', { value: 'MARKET', text: 'MARKET' }),
        el('option', { value: 'LIMIT', text: 'LIMIT (Aggressive)' })
      ]);
      orderTypeSelect.value = p.order_type || 'MARKET';

      var offsetInput = el('input', { type: 'number', step: '0.1', value: p.limit_price_offset_pct === null || p.limit_price_offset_pct === undefined ? '' : p.limit_price_offset_pct, placeholder: '0.0' });
      var productSelect = el('select', {}, [
        el('option', { value: 'MIS', text: 'MIS (intraday)' }),
        el('option', { value: 'NRML', text: 'NRML (carry)' })
      ]);
      productSelect.value = p.product || 'MIS';

      var lotsInput = el('input', { type: 'number', min: '1', value: p.lots || '' });
      var maxLotsInput = el('input', { type: 'number', min: '1', value: p.max_lots || '' });
      var slInput = el('input', { type: 'number', step: '0.1', value: p.default_sl_pct === null || p.default_sl_pct === undefined ? '' : p.default_sl_pct });
      var tgtInput = el('input', { type: 'number', step: '0.1', value: p.default_target_pct === null || p.default_target_pct === undefined ? '' : p.default_target_pct });
      var trailingCheck = el('input', { type: 'checkbox' });
      trailingCheck.checked = !!p.trailing_enabled;
      var trailingStepInput = el('input', { type: 'number', step: '0.1', value: p.trailing_step === null || p.trailing_step === undefined ? '' : p.trailing_step });

      var grid = el('div', { class: 'grid' }, [
        el('div', { class: 'field' }, [el('label', { text: 'Profile Name' }), nameInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Order Type' }), orderTypeSelect]),
        el('div', { class: 'field' }, [el('label', { text: 'Limit Price Offset %' }), offsetInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Product' }), productSelect]),
        el('div', { class: 'field' }, [el('label', { text: 'Lots' }), lotsInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Max Lots' }), maxLotsInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Default SL %' }), slInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Default Target %' }), tgtInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Trailing Step' }), trailingStepInput])
      ]);

      var checkDiv = el('div', { class: 'actions' }, [
        el('label', { class: 'check' }, [trailingCheck, document.createTextNode(' Trail the stop')])
      ]);

      var saveBtn = el('button', { class: 'btn', text: isEdit ? 'Update profile' : 'Create profile' });
      var cancelBtn = el('button', { class: 'btn ghost', text: 'Cancel' });
      var formStatus = el('span', {});

      saveBtn.addEventListener('click', function () {
        var name = nameInput.value.trim();
        if (!name) {
          formStatus.className = 'failed';
          formStatus.textContent = 'Name is required.';
          return;
        }
        var payload = {
          id: p.id,
          name: name,
          order_type: orderTypeSelect.value,
          limit_price_offset_pct: offsetInput.value === '' ? null : Number(offsetInput.value),
          product: productSelect.value,
          lots: lotsInput.value === '' ? null : Number(lotsInput.value),
          max_lots: maxLotsInput.value === '' ? null : Number(maxLotsInput.value),
          default_sl_pct: slInput.value === '' ? null : Number(slInput.value),
          default_target_pct: tgtInput.value === '' ? null : Number(tgtInput.value),
          trailing_enabled: trailingCheck.checked,
          trailing_step: trailingStepInput.value === '' ? null : Number(trailingStepInput.value)
        };

        saveBtn.disabled = true;
        postJSON('/whatsapp-signals/api/profile', payload).then(function (res) {
          if (res.status === 'success') {
            profileFormOpen = false;
            editingProfile = null;
            refresh();
          } else {
            saveBtn.disabled = false;
            formStatus.className = 'failed';
            formStatus.textContent = res.message || 'Could not save profile.';
          }
        });
      });

      cancelBtn.addEventListener('click', function () {
        profileFormOpen = false;
        editingProfile = null;
        renderProfiles();
      });

      var formWrap = el('div', { style: 'border-top: 1px solid var(--line); padding: 14px 18px;' }, [
        grid, checkDiv, el('div', { class: 'actions' }, [saveBtn, cancelBtn, formStatus])
      ]);
      formSlot.appendChild(formWrap);
    }

    if (!profiles.length && !profileFormOpen) {
      host.appendChild(el('p', { class: 'empty', text: 'No order profiles created yet. Create one to assign customized parameters to groups.' }));
      return;
    }

    profiles.forEach(function (prof) {
      var head = el('div', { class: 'profile-head' }, [
        el('span', { class: 'profile-name', text: prof.name }),
        el('div', { class: 'actions', style: 'margin-top:0' }, [
          (function () {
            var btn = el('button', { class: 'btn ghost', text: 'Edit' });
            btn.addEventListener('click', function () {
              editingProfile = prof;
              profileFormOpen = true;
              renderProfiles();
            });
            return btn;
          })(),
          (function () {
            var btn = el('button', { class: 'btn ghost', text: 'Delete' });
            btn.addEventListener('click', function () {
              if (!window.confirm('Delete profile "' + prof.name + '"? Groups using it will revert to group defaults.')) return;
              postJSON('/whatsapp-signals/api/profile/delete', { id: prof.id }).then(refresh);
            });
            return btn;
          })()
        ])
      ]);

      var metaText = 'Type: ' + prof.order_type +
        (prof.limit_price_offset_pct ? ' (' + prof.limit_price_offset_pct + '%)' : '') +
        ' | Lots: ' + (prof.lots || 'default') + ' (max ' + (prof.max_lots || 'default') + ')' +
        ' | Product: ' + (prof.product || 'default') +
        ' | SL: ' + (prof.default_sl_pct ? prof.default_sl_pct + '%' : 'default') +
        ' | TGT: ' + (prof.default_target_pct ? prof.default_target_pct + '%' : 'none') +
        ' | Trailing: ' + (prof.trailing_enabled ? 'Yes (' + (prof.trailing_step || 'default') + ')' : 'No');

      var meta = el('div', { class: 'profile-meta', text: metaText });
      var card = el('div', { class: 'profile-card' }, [head, meta]);
      host.appendChild(card);
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
    var formSlot = document.getElementById('group-form-slot');
    clear(host);
    clear(formSlot);

    if (formSlot && groupFormOpen) {
      var jidInput = el('input', { type: 'text', placeholder: '120363xxxx@g.us, @newsletter or @broadcast JID' });
      var labelInput = el('input', { type: 'text', placeholder: 'e.g. My Signal Group' });
      var grid = el('div', { class: 'grid' }, [
        el('div', { class: 'field' }, [el('label', { text: 'Group JID / Phone' }), jidInput]),
        el('div', { class: 'field' }, [el('label', { text: 'Group Name / Label' }), labelInput])
      ]);
      var addBtn = el('button', { class: 'btn', text: 'Add Group' });
      var cancelBtn = el('button', { class: 'btn ghost', text: 'Cancel' });
      var formStatus = el('span', {});

      addBtn.addEventListener('click', function () {
        var rawJid = jidInput.value.trim();
        if (!rawJid) {
          formStatus.className = 'failed';
          formStatus.textContent = 'Group JID is required.';
          return;
        }
        addBtn.disabled = true;
        postJSON('/whatsapp-signals/api/group', {
          chat_jid: rawJid,
          label: labelInput.value.trim() || null,
          is_enabled: false
        }).then(function (res) {
          if (res.status === 'success') {
            groupFormOpen = false;
            refresh();
          } else {
            addBtn.disabled = false;
            formStatus.className = 'failed';
            formStatus.textContent = res.message || 'Could not add group.';
          }
        });
      });

      cancelBtn.addEventListener('click', function () {
        groupFormOpen = false;
        renderGroups();
      });

      var wrap = el('div', { style: 'border-top: 1px solid var(--line); padding: 14px 18px;' }, [
        grid, el('div', { class: 'actions' }, [addBtn, cancelBtn, formStatus])
      ]);
      formSlot.appendChild(wrap);
    }

    var groups = (state && state.groups) || [];
    if (activeChatJid) {
      groups = groups.filter(function (g) { return g.chat_jid === activeChatJid; });
    }
    if (!groups.length) {
      host.appendChild(el('p', { class: 'empty', text:
        activeChatJid ? 'Selected group not found in record.' :
        'No groups seen yet. When a message is posted in any WhatsApp group on your phone, it will appear here automatically. Or use "+ Add Group JID" above to add a group manually.' }));
      return;
    }

    var profileOptions = [['', 'None (custom group settings below)']];
    (state.profiles || []).forEach(function (p) {
      profileOptions.push([String(p.id), p.name]);
    });

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

      var headChildren = [
        el('span', { class: 'group-name', text: g.label || jid.split('@')[0] }),
        el('span', { class: 'group-jid', text: jid }),
        el('span', { class: 'group-meta', text:
          g.message_count + ' messages seen - last ' + when(g.last_seen_at) })
      ];
      if (g.ai_parser_mode) {
        headChildren.splice(1, 0, el('span', {
          class: 'tag',
          style: 'background: #2563eb; color: #fff; font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 3px;',
          text: 'AI PARSER'
        }));
      }
      headChildren.push(toggle, settingsBtn);
      var head = el('div', { class: 'group-head' }, headChildren);

      var block = el('div', { class: 'group' }, [head]);

      if (open) {
        var grid = el('div', { class: 'grid' }, [
          field(jid, 'label', 'Name', 'text', groupValue(jid, 'label', g.label || '')),
          field(jid, 'order_profile_id', 'Order Profile', 'select',
            groupValue(jid, 'order_profile_id', g.order_profile_id === null ? '' : String(g.order_profile_id)),
            profileOptions),
          field(jid, 'execution_mode', 'Trade in', 'select',
            groupValue(jid, 'execution_mode', g.execution_mode),
            [['analyze', 'Sandbox'], ['live', 'Live money']]),
          field(jid, 'order_type', 'Order Type', 'select',
            groupValue(jid, 'order_type', g.order_type || 'MARKET'),
            [['MARKET', 'MARKET'], ['LIMIT', 'LIMIT']]),
          field(jid, 'limit_price_offset_pct', 'Limit Offset %', 'number',
            groupValue(jid, 'limit_price_offset_pct', g.limit_price_offset_pct)),
          field(jid, 'above_tick_offset', '"Buy above" Tick Offset', 'number',
            groupValue(jid, 'above_tick_offset', g.above_tick_offset !== undefined && g.above_tick_offset !== null ? g.above_tick_offset : 0.5)),
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
          checkbox(jid, 'ai_parser_mode', 'AI Parser (context-aware multi-message reading)', g.ai_parser_mode),
          checkbox(jid, 'trailing_enabled', 'Trail the stop', g.trailing_enabled),
          checkbox(jid, 'llm_fallback', 'Use the model for messages the rules cannot read', g.llm_fallback),
          checkbox(jid, 'auto_apply_ai', 'Auto-apply AI management suggestions immediately', g.auto_apply_ai),
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
          if (Object.prototype.hasOwnProperty.call(changes, 'order_profile_id')) {
            changes.order_profile_id = changes.order_profile_id === '' ? null : Number(changes.order_profile_id);
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
    if (activeChatJid) {
      rows = rows.filter(function (p) { return p.chat_jid === activeChatJid; });
    }
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
    if (activeChatJid) {
      rows = rows.filter(function (e) { return e.chat_jid === activeChatJid; });
    }
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
        el('td', {}, [el('span', { class: 'tag ' + (e.status || ''), text: e.status })]),
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
    renderFilter();
    renderSuggestions();
    renderProfiles();
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

  var newProfBtn = document.getElementById('new-profile-btn');
  if (newProfBtn) {
    newProfBtn.addEventListener('click', function () {
      profileFormOpen = !profileFormOpen;
      editingProfile = null;
      renderProfiles();
    });
  }

  var newGrpBtn = document.getElementById('new-group-btn');
  if (newGrpBtn) {
    newGrpBtn.addEventListener('click', function () {
      groupFormOpen = !groupFormOpen;
      renderGroups();
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
