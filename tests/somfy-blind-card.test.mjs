import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const CMDS = ['identify', 'stop', 'jog_up', 'jog_down', 'set_upper_limit', 'set_lower_limit', 'range_start',
  'set_direction_clockwise', 'set_direction_counterclockwise'];

function element(dataset = {}) {
  const attrs = {};
  const classes = new Set();
  const children = new Map();
  return {
    dataset, hidden: false, disabled: false, textContent: '', value: '', className: '', children: [],
    querySelector: (key) => { if (!children.has(key)) children.set(key, element()); return children.get(key); },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name), has: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    },
    setAttribute: (key, value) => { attrs[key] = value; }, getAttribute: (key) => attrs[key],
    addEventListener() {}, replaceChildren(...children) { this.children = children; },
  };
}

function card() {
  let Card;
  const single = new Map();
  const lists = {
    '[data-cmd]': CMDS.map(cmd => element({ cmd })),
    '[data-step]': [10, 100, 300].map(step => element({ step: String(step) })),
    '[data-tab]': ['setup', 'advanced', 'diagnostics'].map(tab => element({ tab })),
    '[data-panel]': ['setup', 'advanced', 'diagnostics'].map(panel => element({ panel })),
    '[data-detail]': ['contact', 'status', 'last_result'].map(detail => element({ detail })),
  };
  class HTMLElement {
    attachShadow() {
      return {
        set innerHTML(_) {},
        querySelector: (key) => { if (!single.has(key)) single.set(key, element()); return single.get(key); },
        querySelectorAll: (key) => lists[key] ?? [],
        addEventListener() {},
      };
    }
  }
  const window = {};
  runInNewContext(readFileSync(new URL('../custom_components/somfy_ble/frontend/somfy-blind-card.js', import.meta.url), 'utf8'), {
    HTMLElement, window, setTimeout, clearTimeout, Date,
    document: { createElement: () => element() },
    customElements: { get: () => undefined, define: (_, value) => { Card = value; } },
  });
  const instance = new Card();
  instance.setConfig({});
  const button = (cmd) => lists['[data-cmd]'].find(item => item.dataset.cmd === cmd);
  return { instance, single, lists, button };
}

function hass(motor = 'Office Sheer', overrides = {}) {
  const calls = [];
  const prefix = motor.toLowerCase().replace(' ', '_');
  const entities = Object.fromEntries([...CMDS, 'interrogate'].map(cmd => [cmd, `button.${prefix}_${cmd}`]));
  Object.assign(entities, { jog_step: `number.${prefix}`, status: `sensor.${prefix}_status`, last_result: `sensor.${prefix}_result` });
  return {
    calls,
    states: {
      'select.somfy_ble_selected_blind': { state: motor, attributes: { options: ['Kitchen', 'Office Sheer'], entities } },
      [entities.jog_step]: { state: '100' },
      [entities.status]: { state: 'connected' },
      [entities.last_result]: { state: 'success' },
    },
    callService: async (...args) => { calls.push(args); },
    callWS: async () => [],
    ...overrides,
  };
}

test('commands press the selected blind’s button entity', async () => {
  const { instance } = card();
  const h = hass();
  instance.hass = h;
  await instance.command('jog_up');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [['button', 'press', { entity_id: 'button.office_sheer_jog_up' }]]);
});

test('step buttons set the jog-step number and show the current step', async () => {
  const { instance, lists } = card();
  const h = hass();
  instance.hass = h;
  assert.deepEqual(lists['[data-step]'].map(b => b.getAttribute('aria-pressed')), ['false', 'true', 'false']);
  await instance.setStep(300);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [['number', 'set_value', { entity_id: 'number.office_sheer', value: 300 }]]);
});

test('limit and range commands need a second tap', async () => {
  const { instance, single, button } = card();
  const h = hass();
  instance.hass = h;
  await instance.command('set_upper_limit');
  assert.equal(h.calls.length, 0);
  assert.equal(button('set_upper_limit').textContent, 'Tap again to confirm');
  assert.match(single.get('.confirm').textContent, /fully OPEN limit/);
  await instance.command('range_start');
  assert.equal(h.calls.length, 0, 'arming another command must not fire the first');
  await instance.command('range_start');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [['button', 'press', { entity_id: 'button.office_sheer_range_start' }]]);
  assert.equal(single.get('.confirm').hidden, true);
});

test('Stop stays enabled while a command is in flight; others lock', async () => {
  const { instance, button } = card();
  let finish;
  const h = hass('Office Sheer', { callService: () => new Promise(resolve => { finish = resolve; }) });
  instance.hass = h;
  const pending = instance.command('jog_down');
  assert.equal(button('jog_up').disabled, true);
  assert.equal(button('stop').disabled, false);
  finish();
  await pending;
  assert.equal(button('jog_up').disabled, false);
});

test('service failures show on the card', async () => {
  const { instance, single } = card();
  instance.hass = hass('Office Sheer', { callService: async () => { throw new Error('Bluetooth unavailable'); } });
  await instance.command('identify');
  assert.equal(single.get('.error').hidden, false);
  assert.match(single.get('.error').textContent, /Bluetooth unavailable/);
});

test('last result maps to the dashboard alert text', () => {
  const { instance, single } = card();
  const h = hass();
  h.states['sensor.office_sheer_result'] = { state: 'auth_failed' };
  instance.hass = h;
  assert.equal(single.get('ha-alert').hidden, false);
  assert.equal(single.get('ha-alert').getAttribute('alert-type'), 'error');
  assert.match(single.get('ha-alert').textContent, /PIN/);
});

test('inspection presses interrogate and shows that blind’s report', async () => {
  const { instance, single } = card();
  let done = false;
  const h = hass('Office Sheer', {
    callService: async (...args) => {
      assert.deepEqual(JSON.parse(JSON.stringify(args)), ['button', 'press', { entity_id: 'button.office_sheer_interrogate' }]);
      done = true;
    },
    callWS: async () => done ? [
      { title: 'Kitchen · Bluetooth Interrogation', message: 'Wrong blind' },
      { title: 'Office Sheer · Bluetooth Interrogation', message: '**9 services discovered.**' },
    ] : [],
  });
  instance.hass = h;
  await instance.inspect();
  assert.equal(single.get('ha-markdown').content, '**9 services discovered.**');
});

test('switching blinds mid-inspection does not show the previous blind’s report', async () => {
  const { instance, single } = card();
  let finish;
  const h = hass('Office Sheer', {
    callWS: async () => [{ title: 'Office Sheer · Bluetooth Interrogation', message: 'Office report' }],
    callService: () => new Promise(resolve => { finish = resolve; }),
  });
  instance.hass = h;
  await Promise.resolve();
  const pending = instance.inspect();
  instance.hass = hass('Kitchen', { callService: h.callService, callWS: h.callWS });
  finish();
  await pending;
  assert.equal(single.get('ha-markdown').content, '');
});

test('choosing a blind calls select_option; no blinds shows a message', async () => {
  const { instance, single } = card();
  const h = hass();
  instance.hass = h;
  await instance.selectBlind('Kitchen');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [['select', 'select_option', { entity_id: 'select.somfy_ble_selected_blind', option: 'Kitchen' }]]);
  instance.hass = { states: {}, callService: async () => {}, callWS: async () => [] };
  assert.equal(single.get('.status-text').textContent, 'No Somfy blinds configured.');
});

const SPEED = { w: true, l: 1000, h: 2800, u: '1/100rpm', i: 2000 };
const RAMP = { w: true, l: 500, h: 1000, u: 'ms', i: 1000 };
const MOTOR_CONFIG = {
  NominalSpeed: [2000, SPEED],
  StartUpRamp: [1000, RAMP], StartDownRamp: [1000, RAMP],
  StopUpRamp: [1000, RAMP], StopDownRamp: [1000, RAMP],
  DisabledSoftRamps: [false, { w: true, i: false }],
};

function motorHass(writes, { fail } = {}) {
  const h = hass();
  h.callWS = async (msg) => {
    if (msg.type !== 'call_service') return [];
    if (msg.service === 'read_config') return { response: { result: 'success', config: MOTOR_CONFIG } };
    const { key, value } = msg.service_data;
    writes.push([key, value]);
    if (fail === key) return { response: { result: 'error', after: [MOTOR_CONFIG[key][0], {}], confirmed: false } };
    return { response: { result: 'success', after: [value, MOTOR_CONFIG[key][1]], confirmed: true } };
  };
  return h;
}

test('reading motor settings fills the sliders from the motor’s reported ranges', async () => {
  const { instance, single } = card();
  instance.hass = motorHass([]);
  await instance.readSettings();
  const speed = single.get('[data-setting="speed"]').querySelector('input');
  assert.equal(speed.min, 1000);
  assert.equal(speed.max, 2800);
  assert.equal(speed.value, 2000);
  assert.equal(single.get('[data-setting="soft"] input').checked, true);
  assert.equal(single.get('.settings').hidden, false);
  assert.equal(single.get('.save-settings').disabled, true, 'nothing changed yet');
});

test('saving writes only changed settings, and a ramp sets both directions', async () => {
  const { instance, single } = card();
  const writes = [];
  instance.hass = motorHass(writes);
  await instance.readSettings();
  single.get('[data-setting="speed"]').querySelector('input').value = '2800';
  single.get('[data-setting="start"]').querySelector('input').value = '500';
  await instance.saveSettings();
  assert.deepEqual(writes, [['NominalSpeed', 2800], ['StartUpRamp', 500], ['StartDownRamp', 500]]);
  assert.match(single.get('.settings-status').textContent, /Saved/);
});

test('a write the motor did not confirm stops the save and says so', async () => {
  const { instance, single } = card();
  const writes = [];
  instance.hass = motorHass(writes, { fail: 'NominalSpeed' });
  await instance.readSettings();
  single.get('[data-setting="speed"]').querySelector('input').value = '2800';
  single.get('[data-setting="stop"]').querySelector('input').value = '500';
  await instance.saveSettings();
  assert.deepEqual(writes, [['NominalSpeed', 2800]]);
  assert.match(single.get('.settings-status').textContent, /NominalSpeed not saved/);
});

test('turning soft start/stop off writes DisabledSoftRamps true', async () => {
  const { instance, single } = card();
  const writes = [];
  instance.hass = motorHass(writes);
  await instance.readSettings();
  single.get('[data-setting="soft"] input').checked = false;
  await instance.saveSettings();
  assert.deepEqual(writes, [['DisabledSoftRamps', true]]);
});

test('a write the motor did not confirm is reported as unconfirmed, not saved', async () => {
  const { instance, single } = card();
  const h = motorHass([]);
  const base = h.callWS;
  h.callWS = async (msg) => msg.service === 'write_config'
    ? { response: { result: 'success', after: null, confirmed: false } }
    : base(msg);
  instance.hass = h;
  await instance.readSettings();
  single.get('[data-setting="speed"]').querySelector('input').value = '2800';
  await instance.saveSettings();
  assert.match(single.get('.settings-status').textContent, /unconfirmed/);
});

test('a refused write shows the integration’s reason', async () => {
  const { instance, single } = card();
  const h = motorHass([]);
  const base = h.callWS;
  h.callWS = async (msg) => msg.service === 'write_config'
    ? { response: { result: 'invalid_payload', error: 'NominalSpeed must be between 1000 and 2800' } }
    : base(msg);
  instance.hass = h;
  await instance.readSettings();
  single.get('[data-setting="speed"]').querySelector('input').value = '2800';
  await instance.saveSettings();
  assert.match(single.get('.settings-status').textContent, /between 1000 and 2800/);
});

test('motor settings read only when Advanced opens, once per blind, never on load', async () => {
  const { instance } = card();
  const reads = [];
  const h = motorHass([]);
  const base = h.callWS;
  let release;
  h.callWS = async (msg) => {
    if (msg.service === 'read_config') { reads.push(msg.service_data.motor); await new Promise(r => { release = r; }); }
    return base(msg);
  };
  instance.hass = h;
  instance.hass = { ...h };
  assert.deepEqual(reads, [], 'no read on load or hass updates');
  instance.showTab('diagnostics');
  assert.deepEqual(reads, []);
  instance.showTab('advanced');
  await Promise.resolve();
  instance.showTab('setup'); instance.showTab('advanced');
  instance.readSettings();
  assert.deepEqual(reads, ['Office Sheer'], 'no second read while one is in flight');
  // Blind changes mid-read: the stale result is dropped and the new blind is read next.
  const kitchen = motorHass([]);
  kitchen.states = { ...kitchen.states, 'select.somfy_ble_selected_blind': { ...kitchen.states['select.somfy_ble_selected_blind'], state: 'Kitchen' } };
  kitchen.callWS = h.callWS;
  instance.hass = kitchen;
  release();
  for (let i = 0; i < 10; i++) await Promise.resolve();
  assert.deepEqual(reads, ['Office Sheer', 'Kitchen']);
  release();
});
