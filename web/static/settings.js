// settings.js — loaded TRƯỚC app.js trong index.html
(function() {
  'use strict';
  const MIGRATED_KEY = 'gpt_reg.settings_migrated_v1';
  const LS_KEYS_TO_MIGRATE = [
    'gpt_reg.settings',
    'gpt_reg.mail_mode', 'gpt_reg.worker_config', 'gpt_reg.active_tab',
    'autoreg.config.v1', 'hme.privacy.mask.v1', 'gpt_reg.link.mode'
  ];

  window.Settings = {
    _cache: null,

    async bootstrap(token) {
      if (!localStorage.getItem(MIGRATED_KEY)) {
        const snapshot = {};
        LS_KEYS_TO_MIGRATE.forEach(k => {
          const v = localStorage.getItem(k);
          if (v !== null) snapshot[k] = v;
        });
        try {
          const resp = await fetch('/api/settings/import-from-localstorage', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-API-Token': token},
            body: JSON.stringify({localstorage: snapshot}),
          });
          if (resp.ok) {
            const data = await resp.json();
            (data.client_keys_to_remove || []).forEach(k => localStorage.removeItem(k));
            localStorage.setItem(MIGRATED_KEY, '1');
          }
        } catch(e) { console.warn('[settings] migration failed:', e); }
      }
      return this.load(token);
    },

    async load(token) {
      try {
        const resp = await fetch('/api/settings', {
          headers: {'X-API-Token': token}
        });
        if (resp.ok) {
          const data = await resp.json();
          this._cache = data.settings || {};
        }
      } catch(e) { console.warn('[settings] load failed:', e); }
      return this._cache || {};
    },

    get(key) { return this._cache ? this._cache[key] : undefined; },

    async save(key, value, token) {
      try {
        await fetch('/api/settings/' + encodeURIComponent(key), {
          method: 'PUT',
          headers: {'Content-Type': 'application/json', 'X-API-Token': token},
          body: JSON.stringify({value}),
        });
        if (this._cache) this._cache[key] = value;
      } catch(e) { console.warn('[settings] save failed:', e); }
    }
  };
})();
