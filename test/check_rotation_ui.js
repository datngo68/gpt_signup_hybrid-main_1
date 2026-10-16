/* Offline Session UI checks: real source, mocked DOM/API, no live rotations. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/static/session.js', 'utf8');
const nodes = new Map();
const node = (id) => {
  if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', innerHTML: '', addEventListener() {}, querySelectorAll: () => [], appendChild() {}, style: {} });
  return nodes.get(id);
};
const requests = [];
const context = {
  console, Map, Set, Blob, URL,
  document: { getElementById: node },
  localStorage: { getItem: () => null, setItem() {} },
  MutationObserver: class { observe() {} },
  setTimeout() {}, setInterval() {},
  SseBus: { on() {} },
  window: { GptUi: { getAuthToken: () => '', icon: () => '', persistTextarea() {} }, confirm: () => true },
  fetch: async (path, opts) => {
    requests.push({ path, opts });
    return { ok: true, json: async () => path.endsWith('/secrets') ? { secrets: { good: { password: 'password', secret: 'NEW' } } } : { items: [] } };
  },
};
const instrumented = source.replace('  // ── Init', '  window.check = { state, secretsCache, _pastedSecretsByEmail, _resolveSecretsFor, sessionCache };\n  // ── Init');
vm.runInNewContext(instrumented, context);
(async () => {
  const api = context.window.SessionRotation;
  await Promise.resolve();
  const initial = requests.length;
  await api.enqueue([]);
  assert.equal(requests.length, initial);
  await api.enqueue(['good']);
  assert.deepEqual(JSON.parse(requests.at(-1).opts.body).job_ids, ['good']);
  const check = context.window.check;
  check._pastedSecretsByEmail.set('test@example.invalid', { password: 'password', secret: 'OLD' });
  check.sessionCache.set('good', { two_factor: { secret: 'OLD' } });
  await api.refresh();
  assert.equal(check.sessionCache.size, 0);
  assert.equal(check._resolveSecretsFor({ id: 'good', email: 'test@example.invalid' }).secret, 'NEW');
  check._pastedSecretsByEmail.set('test@example.invalid', { password: 'password', secret: 'OLD' });
  assert.equal(check._resolveSecretsFor({ id: 'good', email: 'test@example.invalid' }).secret, 'NEW');
  const bulk = fs.readFileSync('web/static/bulk.js', 'utf8');
  assert(bulk.includes('data-bulk-action="rotate" disabled'));
  assert(bulk.includes("rows.filter((row) => statusOf(row) === 'success').map"));
  assert(!source.includes('window.prompt'));
  console.log('PASS: empty/selected enqueue, fresh authoritative output, stale cache invalidation, selected successful toolbar');
})().catch((error) => { console.error(error); process.exitCode = 1; });
