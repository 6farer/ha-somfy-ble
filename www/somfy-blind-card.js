// One card for Somfy BLE calibration: pick a blind, jog, save limits, range setup, inspect.
// Entity IDs come from select.somfy_ble_selected_blind's `entities` attribute, so no
// script or helper is needed. Messages match the somfy-blinds dashboard.

const SELECT = 'select.somfy_ble_selected_blind';
const STEPS = [['Fine', 10], ['Normal', 100], ['Coarse', 300]];
const STATUS = {
  connected: ['ok', 'Connected'], ready: ['ok', 'Ready'], idle: ['idle', 'Idle · connects on next command'],
  sent_unconfirmed: ['warn', 'Sent, unconfirmed'], degraded: ['warn', 'Degraded'], busy: ['warn', 'Busy'],
  offline: ['bad', 'Offline'],
};
const RESULTS = {
  unknown_after_write: ['warning', 'Command sent; outcome uncertain. Check the blind before trying again.'],
  sent_unconfirmed: ['warning', 'Command sent; outcome uncertain. Check the blind before trying again.'],
  not_found: ['error', 'The blind or its control could not be reached. Identify the blind, try a new request.'],
  timeout_before_write: ['warning', 'Connection timed out before sending the command. Try Identify to check the blind.'],
  auth_failed: ['error', 'Authentication failed. Check this motor’s PIN in the integration settings.'],
  busy: ['info', 'Bluetooth is busy. Wait for the current request to finish.'],
  queue_full: ['info', 'Bluetooth is busy. Wait for the current request to finish.'],
  owner_conflict: ['info', 'Bluetooth is busy. Wait for the current request to finish.'],
  expired: ['warning', 'The request expired. Try a new adjustment.'],
  disconnected: ['warning', 'Bluetooth disconnected. Try Identify to check the blind.'],
  error: ['error', 'The request failed. Open Diagnostics for details.'],
  invalid_payload: ['error', 'The request failed. Open Diagnostics for details.'],
  stale_callback: ['error', 'The request failed. Open Diagnostics for details.'],
  preempted: ['info', 'The queued request was cancelled by Stop.'],
};
const CONFIRM = {
  set_upper_limit: 'Save the current position as the fully OPEN limit for the selected blind?',
  set_lower_limit: 'Save the current position as the fully CLOSED limit for the selected blind?',
  range_start: 'Begin full range setup for the selected blind? Keep the blind in view. Use this only when commissioning its full range.',
  set_direction_clockwise: 'Change the selected motor direction to clockwise?',
  set_direction_counterclockwise: 'Change the selected motor direction to counterclockwise?',
};
// Motor settings from the config file; ramps write both directions together.
const SETTINGS = [
  { id: 'speed', label: 'Speed', keys: ['NominalSpeed'], step: 100, format: (v) => `${v / 100} rpm` },
  { id: 'start', label: 'Soft start', keys: ['StartUpRamp', 'StartDownRamp'], step: 50, format: (v) => `${v / 1000} s` },
  { id: 'stop', label: 'Soft stop', keys: ['StopUpRamp', 'StopDownRamp'], step: 50, format: (v) => `${v / 1000} s` },
];
const SOFT_RAMPS_OFF = 'DisabledSoftRamps';
// The integration refuses writes without a read of the same motor in the last 15 minutes.
const READ_FRESH_MS = 14 * 60 * 1000;
const CONFIRM_MS = 5000;
const INSPECT_LABEL = 'Inspect Bluetooth services';

function contactAge(state, now) {
  const value = Number(state?.state);
  if (!state || !Number.isFinite(value) || value < 0) return 'none yet';
  const seconds = Math.max(0, Math.floor((now - Date.parse(state.last_updated)) / 1000 + value));
  if (seconds < 60) return 'less than a minute ago';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

class SomfyBlindCard extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        ha-card { padding: 16px; display: grid; gap: 12px; }
        button { font: inherit; color: var(--primary-text-color); background: var(--secondary-background-color);
          border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px 14px; cursor: pointer;
          min-height: 44px; }
        button:focus-visible, select:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button:disabled { cursor: wait; opacity: .5; }
        button.armed { border-color: var(--warning-color, #ffa600); color: var(--warning-color, #ffa600); }
        button.stop { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); font-weight: 600; }
        .head { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .field { grid-column: 1 / -1; display: grid; gap: 6px; }
        .picker { position: relative; display: grid; }
        select { appearance: none; font: inherit; font-size: 16px; color: var(--primary-text-color);
          background: var(--input-fill-color, transparent);
          border: 1px solid var(--input-outlined-idle-border-color, var(--secondary-text-color)); border-radius: 6px;
          padding: 0 40px 0 12px; min-height: 48px; min-width: 0; cursor: pointer; }
        select:hover { border-color: var(--input-outlined-hover-border-color, var(--primary-text-color)); }
        option { background: var(--card-background-color); color: var(--primary-text-color); }
        .picker svg { position: absolute; right: 12px; top: 50%; translate: 0 -50%; width: 20px; height: 20px;
          fill: var(--secondary-text-color); pointer-events: none; }
        .status { display: flex; align-items: center; gap: 8px; color: var(--secondary-text-color); font-size: 14px; }
        .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; background: var(--disabled-text-color, #888); }
        .dot.ok { background: var(--success-color, #43a047); }
        .dot.warn { background: var(--warning-color, #ffa600); }
        .dot.bad { background: var(--error-color, #db4437); }
        .error, .confirm { margin: 0; line-height: 1.4; }
        .error { color: var(--error-color, #db4437); }
        .confirm { color: var(--warning-color, #ffa600); }
        .tabs { display: grid; grid-template-columns: repeat(3, 1fr); border-bottom: 1px solid var(--divider-color); }
        .tabs button { background: none; border: 0; border-radius: 0; border-bottom: 2px solid transparent;
          color: var(--secondary-text-color); }
        .tabs button[aria-selected="true"] { color: var(--primary-text-color); font-weight: 600;
          border-bottom-color: var(--primary-color); }
        section { display: grid; gap: 10px; }
        .label { font-size: 13px; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: .04em; }
        .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 8px; }
        .seg button[aria-pressed="true"] { background: var(--primary-color); color: var(--text-primary-color, #fff);
          border-color: var(--primary-color); }
        .jog button { font-size: 16px; padding: 18px 8px; }
        .hint { margin: 0; color: var(--secondary-text-color); font-size: 14px; line-height: 1.4; }
        dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 0; font-size: 14px; }
        dt { color: var(--secondary-text-color); }
        dd { margin: 0; overflow-wrap: anywhere; }
        ha-markdown { display: block; overflow-wrap: anywhere; max-height: 360px; overflow-y: auto; }
        .settings { display: grid; gap: 14px; }
        .setting { display: grid; gap: 4px; }
        .setting-head { display: flex; justify-content: space-between; font-size: 15px; }
        .setting-head output { font-variant-numeric: tabular-nums; color: var(--secondary-text-color); }
        .setting-head output.changed { color: var(--primary-color); font-weight: 600; }
        input[type="range"] { width: 100%; accent-color: var(--primary-color); min-height: 32px; }
        .toggle { display: flex; align-items: center; gap: 10px; font-size: 15px; min-height: 44px; }
        .toggle input { width: 20px; height: 20px; accent-color: var(--primary-color); }
        .section-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .section-head.commissioning { margin-top: 12px; padding-top: 14px; border-top: 1px solid var(--divider-color); }
        button.small { min-height: 32px; padding: 4px 14px; font-size: 13px; border-radius: 16px; }
        [hidden] { display: none !important; }
      </style>
      <ha-card>
        <div class="head">
          <label class="field"><span class="label">Blind</span>
            <span class="picker"><select></select><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5z"/></svg></span>
          </label>
          <button type="button" data-cmd="identify">Identify</button>
          <button type="button" data-cmd="stop" class="stop">■ Stop</button>
        </div>
        <div class="status"><span class="dot"></span><span class="status-text"></span></div>
        <ha-alert hidden></ha-alert>
        <p class="error" role="alert" hidden></p>
        <p class="confirm" aria-live="polite" hidden></p>
        <div class="tabs" role="tablist">
          <button type="button" role="tab" data-tab="setup">Setup</button>
          <button type="button" role="tab" data-tab="advanced">Advanced</button>
          <button type="button" role="tab" data-tab="diagnostics">Diagnostics</button>
        </div>
        <section data-panel="setup">
          <div class="label">Step</div>
          <div class="row seg">${STEPS.map(([name, value]) =>
            `<button type="button" data-step="${value}">${name} ${value}</button>`).join('')}</div>
          <div class="row jog">
            <button type="button" data-cmd="jog_up">▲ Jog up</button>
            <button type="button" data-cmd="jog_down">▼ Jog down</button>
          </div>
          <div class="label">Limits</div>
          <div class="row">
            <button type="button" data-cmd="set_upper_limit">Save open limit</button>
            <button type="button" data-cmd="set_lower_limit">Save closed limit</button>
          </div>
        </section>
        <section data-panel="advanced">
          <div class="section-head"><span class="label">Movement</span><button type="button" class="read-settings small">Refresh</button></div>
          <p class="hint settings-status" aria-live="polite"></p>
          <div class="settings" hidden>
            ${SETTINGS.map(({ id, label }) => `<label class="setting" data-setting="${id}">
              <span class="setting-head"><span>${label}</span><output></output></span>
              <input type="range">
              <span class="hint setting-range"></span>
            </label>`).join('')}
            <label class="toggle" data-setting="soft"><input type="checkbox"> Soft start and stop</label>
            <button type="button" class="save-settings">Save changes</button>
          </div>
          <div class="section-head commissioning"><span class="label">Commissioning</span></div>
          <p class="hint">Use these only for a motor with no valid range or the wrong direction. For an endpoint adjustment, use Setup.</p>
          <button type="button" data-cmd="range_start">Begin range setup</button>
          <div class="label">Direction</div>
          <div class="row">
            <button type="button" data-cmd="set_direction_clockwise">Clockwise</button>
            <button type="button" data-cmd="set_direction_counterclockwise">Counterclockwise</button>
          </div>
        </section>
        <section data-panel="diagnostics">
          <button type="button" class="inspect">${INSPECT_LABEL}</button>
          <p class="hint inspect-status" aria-live="polite"></p>
          <ha-markdown tabindex="0" aria-label="Bluetooth inspection report"></ha-markdown>
          <dl>
            <dt>Last contact</dt><dd data-detail="contact"></dd>
            <dt>Runtime</dt><dd data-detail="status"></dd>
            <dt>Last result</dt><dd data-detail="last_result"></dd>
            <dt>Last error</dt><dd data-detail="last_error"></dd>
            <dt>Bluetooth source</dt><dd data-detail="selected_source"></dd>
            <dt>Last command ID</dt><dd data-detail="last_command_id"></dd>
          </dl>
          <p class="hint">Inspection lists services without sending a PIN or motor commands. Last contact does not mean an active connection.</p>
        </section>
      </ha-card>`;
    const $ = (selector) => root.querySelector(selector);
    const $$ = (selector) => [...root.querySelectorAll(selector)];
    this._el = {
      select: $('select'), dot: $('.dot'), status: $('.status-text'), alert: $('ha-alert'), error: $('.error'),
      confirm: $('.confirm'), inspect: $('.inspect'), inspectStatus: $('.inspect-status'), report: $('ha-markdown'),
      buttons: Object.fromEntries($$('[data-cmd]').map(button => [button.dataset.cmd, button])),
      steps: $$('[data-step]'), tabs: $$('[data-tab]'), panels: $$('[data-panel]'), details: $$('[data-detail]'),
      readSettings: $('.read-settings'), saveSettings: $('.save-settings'), settingsStatus: $('.settings-status'),
      settingsBox: $('.settings'), soft: $('[data-setting="soft"]'), softInput: $('[data-setting="soft"] input'),
      settings: Object.fromEntries(SETTINGS.map(({ id }) => {
        const row = $(`[data-setting="${id}"]`);
        return [id, { row, input: row.querySelector('input'), output: row.querySelector('output'), range: row.querySelector('.setting-range') }];
      })),
    };
    this._pending = 0;
    this._tab = 'setup';
    root.addEventListener('click', (event) => {
      const button = event.target.closest?.('button');
      if (!button || button.disabled) return;
      if (button.dataset.cmd) this.command(button.dataset.cmd);
      else if (button.dataset.step) this.setStep(Number(button.dataset.step));
      else if (button.dataset.tab) this.showTab(button.dataset.tab);
      else if (button === this._el.inspect) this.inspect();
      else if (button === this._el.readSettings) this.readSettings();
      else if (button === this._el.saveSettings) this.saveSettings();
    });
    root.addEventListener('input', () => this._renderSettings());
    root.addEventListener('change', () => this._renderSettings());
    this._el.select.addEventListener('change', () => this.selectBlind(this._el.select.value));
    this.showTab('setup');
  }

  setConfig(config) { this._config = config ?? {}; }
  getCardSize() { return 7; }
  getGridOptions() { return { columns: 12, min_columns: 6 }; }

  get _selectId() { return this._config?.entity ?? SELECT; }

  set hass(hass) {
    this._hass = hass;
    this._el.report.hass = hass;
    const motor = hass.states[this._selectId]?.state;
    if (motor !== this._motor) {
      this._motor = motor;
      this._disarm();
      this._showError('');
      this._report('', 'Inspect to see this blind’s Bluetooth services here.');
      this._config = null;
      this._readAt = undefined;
      this._el.settingsBox.hidden = true;
      this._el.settingsStatus.textContent = '';
      // Only Advanced reads the motor: each read holds the Bluetooth lock and wakes the motor.
      if (this._tab === 'advanced') this.readSettings();
      this._loadPrevious(motor);
    }
    this._render();
  }

  _entities() { return this._hass?.states[this._selectId]?.attributes?.entities ?? {}; }
  _state(key) { return this._hass?.states[this._entities()[key]]; }

  _render() {
    const select = this._hass?.states[this._selectId];
    const options = select?.attributes?.options ?? [];
    const el = this._el;
    if (options.join('\n') !== this._options) {
      this._options = options.join('\n');
      el.select.replaceChildren(...options.map(option => Object.assign(document.createElement('option'), { value: option, textContent: option })));
    }
    if (el.select.value !== this._motor) el.select.value = this._motor ?? '';

    const configured = Boolean(this._motor && this._entities().stop);
    const status = this._state('status')?.state;
    const reachable = this._state('ble_reachable')?.state;
    const [tone, label] = STATUS[status] ?? ['idle', 'Unknown'];
    el.dot.className = `dot ${reachable === 'off' ? 'bad' : tone}`;
    el.status.textContent = configured
      ? `${label} · ${reachable === 'on' ? 'reachable' : reachable === 'off' ? 'not reachable' : 'reachability unknown'}`
        + ` · last contact ${contactAge(this._state('last_contact_age'), Date.now())}`
      : 'No Somfy blinds configured.';

    const result = this._state('last_result')?.state;
    const alert = RESULTS[result];
    el.alert.hidden = !configured || !alert;
    if (alert) { el.alert.setAttribute('alert-type', alert[0]); el.alert.textContent = alert[1]; }

    const step = Number(this._state('jog_step')?.state);
    for (const button of el.steps) button.setAttribute('aria-pressed', String(Number(button.dataset.step) === step));

    const locked = !configured || this._pending > 0 || status === 'busy';
    for (const [cmd, button] of Object.entries(el.buttons)) button.disabled = cmd === 'stop' ? !configured : locked;
    for (const button of el.steps) button.disabled = locked;
    el.inspect.disabled = locked || this._inspecting;
    el.readSettings.disabled = locked;
    this._renderSettings();

    const contact = contactAge(this._state('last_contact_age'), Date.now());
    for (const dd of el.details) {
      dd.textContent = dd.dataset.detail === 'contact' ? contact : this._state(dd.dataset.detail)?.state ?? '—';
    }
  }

  // Desired value per config key from the controls, for keys the motor reports as writable.
  _desired() {
    const desired = {};
    if (!this._config) return desired;
    for (const { id, keys } of SETTINGS) {
      const value = Number(this._el.settings[id].input.value);
      for (const key of keys) if (this._writable(key)) desired[key] = value;
    }
    if (this._writable(SOFT_RAMPS_OFF)) desired[SOFT_RAMPS_OFF] = !this._el.softInput.checked;
    return desired;
  }

  _writable(key) { return this._config?.[key]?.[1]?.w === true; }
  _current(key) { return this._config?.[key]?.[0]; }

  _changes() {
    return Object.entries(this._desired()).filter(([key, value]) => this._current(key) !== value);
  }

  _renderSettings() {
    const el = this._el;
    const changed = new Set(this._changes().map(([key]) => key));
    for (const { id, keys, format } of SETTINGS) {
      const { input, output } = el.settings[id];
      output.textContent = this._config ? format(Number(input.value)) : '';
      output.classList.toggle('changed', keys.some(key => changed.has(key)));
    }
    const locked = this._pending > 0 || this._state('status')?.state === 'busy';
    for (const { input } of Object.values(el.settings)) input.disabled = locked;
    el.softInput.disabled = locked;
    // Count controls, not keys: one ramp slider writes both directions.
    const controls = SETTINGS.filter(({ keys }) => keys.some(key => changed.has(key))).length
      + (changed.has(SOFT_RAMPS_OFF) ? 1 : 0);
    el.saveSettings.disabled = locked || !changed.size;
    el.saveSettings.textContent = controls ? `Save ${controls} change${controls > 1 ? 's' : ''}` : 'No changes';
  }

  _showSettings(config) {
    this._config = config;
    const el = this._el;
    for (const { id, keys, step, format } of SETTINGS) {
      const { row, input, range } = el.settings[id];
      const key = keys.find(item => this._writable(item));
      row.hidden = !key;
      if (!key) continue;
      const { l, h, i } = config[key][1];
      Object.assign(input, { min: l, max: h, step, value: config[key][0] });
      const others = keys.filter(item => item !== key && this._writable(item) && this._current(item) !== this._current(key));
      range.textContent = `${format(l)} – ${format(h)}${i != null ? ` · default ${format(i)}` : ''}`
        + (others.length ? ' · up and down differ; saving sets both' : '');
    }
    el.soft.hidden = !this._writable(SOFT_RAMPS_OFF);
    el.softInput.checked = this._current(SOFT_RAMPS_OFF) === false;
    el.settingsBox.hidden = false;
    this._renderSettings();
  }

  async _configService(service, data) {
    const { response } = await this._hass.callWS({
      type: 'call_service', domain: 'somfy_ble', service, service_data: data, return_response: true,
    });
    return response;
  }

  async _withPending(work) {
    this._pending++;
    this._showError('');
    this._render();
    try {
      return await work();
    } finally {
      this._pending--;
      this._render();
    }
  }

  async readSettings() {
    const motor = this._motor;
    if (!motor || !this._hass) return;
    // One read at a time; a blind change mid-read is picked up when it finishes.
    if (this._reading) return;
    this._reading = motor;
    this._el.readSettings.textContent = 'Reading…';
    this._el.settingsStatus.textContent = `Reading ${motor}’s settings…`;
    await this._withPending(async () => {
      try {
        const response = await this._configService('read_config', { motor });
        if (motor !== this._motor) return;
        if (response?.result !== 'success' || !response.config) throw new Error(response?.error ?? response?.result ?? 'no reply');
        this._readAt = Date.now();
        this._showSettings(response.config);
        this._el.settingsStatus.textContent = `Read from ${motor}.`;
      } catch (error) {
        if (motor === this._motor) this._el.settingsStatus.textContent = `Couldn’t read settings: ${error.message ?? error}`;
      }
    });
    this._reading = null;
    this._el.readSettings.textContent = 'Refresh';
    if (this._tab === 'advanced' && this._motor !== motor) this.readSettings();
  }

  async saveSettings() {
    const motor = this._motor;
    const changes = this._changes();
    if (!changes.length) return;
    await this._withPending(async () => {
      if (!(Date.now() - this._readAt < READ_FRESH_MS)) {
        this._el.settingsStatus.textContent = `Re-reading ${motor}…`;
        const response = await this._configService('read_config', { motor }).catch(error => ({ error: error.message }));
        if (motor !== this._motor) return;
        if (response?.result !== 'success' || !response.config) {
          this._el.settingsStatus.textContent = `Couldn’t read settings: ${response?.error ?? response?.result ?? 'no reply'}`;
          return;
        }
        this._readAt = Date.now();
      }
      for (const [index, [key, value]] of changes.entries()) {
        this._el.settingsStatus.textContent = `Saving ${index + 1} of ${changes.length}…`;
        try {
          const response = await this._configService('write_config', { motor, key, value });
          if (motor !== this._motor) return;
          // Show what the motor actually has; only a confirmed re-read counts as saved.
          if (Array.isArray(response?.after)) this._config = { ...this._config, [key]: response.after };
          if (response?.confirmed !== true) {
            const after = response?.after?.[0];
            throw new Error(response?.error
              ?? (after !== undefined && after !== value ? `motor reports ${after}` : 'unconfirmed, the motor could not be re-read'));
          }
        } catch (error) {
          if (motor === this._motor) this._el.settingsStatus.textContent = `${key} not saved: ${error.message ?? error}`;
          return;
        }
      }
      this._el.settingsStatus.textContent = `Saved to ${motor}.`;
    });
  }

  showTab(tab) {
    this._disarm();
    const opened = tab === 'advanced' && this._tab !== 'advanced';
    this._tab = tab;
    if (opened && !(Date.now() - this._readAt < READ_FRESH_MS)) this.readSettings();
    for (const button of this._el.tabs) button.setAttribute('aria-selected', String(button.dataset.tab === tab));
    for (const panel of this._el.panels) panel.hidden = panel.dataset.panel !== tab;
  }

  _showError(message) {
    this._el.error.hidden = !message;
    this._el.error.textContent = message;
  }

  _disarm() {
    clearTimeout(this._armed?.timer);
    if (this._armed) {
      const button = this._el.buttons[this._armed.cmd];
      button.classList.remove('armed');
      button.textContent = this._armed.label;
    }
    this._armed = null;
    this._el.confirm.hidden = true;
  }

  async _call(domain, service, data) {
    this._pending++;
    this._showError('');
    this._render();
    try {
      await this._hass.callService(domain, service, data);
      return true;
    } catch (error) {
      this._showError(`${service.replace('_', ' ')} failed: ${error.message ?? error}`);
      return false;
    } finally {
      this._pending--;
      this._render();
    }
  }

  // Confirm-gated commands need a second tap within CONFIRM_MS.
  async command(cmd) {
    const entityId = this._entities()[cmd];
    if (!entityId) return;
    if (CONFIRM[cmd] && this._armed?.cmd !== cmd) {
      this._disarm();
      const button = this._el.buttons[cmd];
      this._armed = { cmd, label: button.textContent, timer: setTimeout(() => this._disarm(), CONFIRM_MS) };
      button.classList.add('armed');
      button.textContent = 'Tap again to confirm';
      this._el.confirm.textContent = CONFIRM[cmd];
      this._el.confirm.hidden = false;
      return;
    }
    this._disarm();
    await this._call('button', 'press', { entity_id: entityId });
  }

  async setStep(value) {
    const entityId = this._entities().jog_step;
    if (entityId) await this._call('number', 'set_value', { entity_id: entityId, value });
  }

  async selectBlind(option) {
    if (option && option !== this._motor) await this._call('select', 'select_option', { entity_id: this._selectId, option });
  }

  _report(content, status) {
    this._el.report.content = content;
    this._el.inspectStatus.textContent = status;
  }

  async _readReport(motor) {
    const notifications = await this._hass.callWS({ type: 'persistent_notification/get' });
    return notifications.find(item => item.title === `${motor} · Bluetooth Interrogation`);
  }

  async _loadPrevious(motor) {
    const inspection = this._inspection;
    try {
      const report = await this._readReport(motor);
      if (this._motor !== motor || this._inspecting || this._inspection !== inspection || !report) return;
      this._report(report.message, `Last inspection · ${motor}`);
    } catch {
      // Inspect remains available to retry and show a visible error.
    }
  }

  async inspect() {
    const entityId = this._entities().interrogate;
    if (this._inspecting || !entityId) return;
    this._inspection = (this._inspection ?? 0) + 1;
    const motor = this._motor;
    this._inspecting = true;
    this._el.inspect.textContent = 'Inspecting…';
    this._report('', `Inspecting ${motor}…`);
    this._render();
    try {
      await this._hass.callService('button', 'press', { entity_id: entityId });
      const report = await this._readReport(motor);
      if (motor !== this._motor) return;
      if (!report) throw new Error('No inspection report returned. Try again.');
      this._report(report.message, `Inspection result · ${motor}`);
    } catch (error) {
      if (motor === this._motor) this._report('', `Inspection failed: ${error.message ?? error}`);
    } finally {
      this._inspecting = false;
      this._el.inspect.textContent = INSPECT_LABEL;
      this._render();
    }
  }
}

if (!customElements.get('somfy-blind-card')) {
  customElements.define('somfy-blind-card', SomfyBlindCard);
  window.customCards = window.customCards ?? [];
  window.customCards.push({ type: 'somfy-blind-card', name: 'Somfy blind', description: 'Somfy BLE calibration: jog, limits, range setup, motor speed and soft start/stop, inspection.' });
}
