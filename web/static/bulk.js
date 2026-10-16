/* Shared bulk selection/actions for Reg, Session, Link and UPI job lists. */
(() => {
  'use strict';

  const configs = [
    { tab: 'tab-reg', list: 'job-list', success: 'success-pane', error: 'error-pane' },
    { tab: 'tab-session', list: 'ses-job-list', success: ['ses-free-pane', 'ses-plus-pane'], error: 'ses-error-pane', session: true },
    { tab: 'tab-link', list: 'link-job-list', error: 'link-error-pane', link: true },
    { tab: 'tab-upi', list: 'upi-job-list', success: 'upi-success-pane', error: 'upi-error-pane', plus: true },
  ];

  const token = () => window.GptUi.getAuthToken();
  const toast = (message, type = 'success') => window.GptUi.toast(message, { type });

  function selectedRows(list) {
    return [...list.querySelectorAll('.job')].filter((row) => row.querySelector('.bulk-check')?.checked);
  }

  function statusOf(row) {
    const node = row.querySelector('.job-status');
    return node ? node.textContent.trim().toLowerCase() : '';
  }

  function emailOf(row) {
    const node = row.querySelector('.job-email-text, .job-email');
    return node ? node.textContent.replace(/\[[^\]]+\]\s*/g, '').trim().split(/\s+/)[0] : '';
  }

  function linesForEmails(ids, emails) {
    const wanted = new Set(emails.map((v) => v.toLowerCase()));
    return ids.flatMap((id) => {
      const pane = document.getElementById(id);
      if (!pane) return [];
      return pane.textContent.split('\n').filter((line) => wanted.has(line.trim().split(/[|\s]/)[0].toLowerCase()));
    });
  }

  function download(name, text, type = 'text/plain') {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function fetchSession(id) {
    const headers = token() ? { 'X-API-Token': token() } : {};
    const response = await fetch(`/api/session/jobs/${encodeURIComponent(id)}`, { headers });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    return data.session_data || null;
  }

  function mount(config) {
    const list = document.getElementById(config.list);
    if (!list) return;
    const toolbar = document.createElement('div');
    toolbar.className = 'bulk-toolbar';
    toolbar.innerHTML = `
      <span class="bulk-count">Đã chọn 0</span>
      <button class="btn btn-ghost btn-small" data-bulk-select="all">Tất cả</button>
      <button class="btn btn-ghost btn-small" data-bulk-select="success">Success</button>
      <button class="btn btn-ghost btn-small" data-bulk-select="error">Failed</button>
      ${config.session ? '<button class="btn btn-ghost btn-small" data-bulk-select="plus">Plus</button><button class="btn btn-ghost btn-small" data-bulk-select="free">Free</button>' : ''}
      <button class="btn btn-ghost btn-small" data-bulk-select="running">Running</button>
      <button class="btn btn-ghost btn-small" data-bulk-select="invert">Đảo chọn</button>
      <button class="btn btn-ghost btn-small" data-bulk-select="none">Bỏ chọn</button>
      <button class="btn btn-primary btn-small" data-bulk-action="copy">Copy</button>
      ${config.session ? '<button class="btn btn-ghost btn-small" data-bulk-action="json">Copy JSON</button><button class="btn btn-primary btn-small" data-bulk-action="rotate" disabled title="Đổi TOTP cho tài khoản thành công đã chọn">Rotate TOTP</button>' : ''}
      <button class="btn btn-ghost btn-small" data-bulk-action="export">Export</button>
      <button class="btn btn-ghost btn-small" data-bulk-action="retry">Retry lỗi</button>
      <button class="btn btn-ghost btn-small" data-bulk-action="remove">Xóa đã xong</button>`;
    list.before(toolbar);

    const selection = new Set();
    const update = () => {
      list.querySelectorAll('.job').forEach((row) => {
        if (row.querySelector('.bulk-check')?.checked) selection.add(row.dataset.id);
        else selection.delete(row.dataset.id);
      });
      const count = selectedRows(list).length;
      toolbar.querySelector('.bulk-count').textContent = `Đã chọn ${count}`;
      toolbar.classList.toggle('has-selection', count > 0);
      const rotate = toolbar.querySelector('[data-bulk-action="rotate"]');
      if (rotate) rotate.disabled = !selectedRows(list).some((row) => statusOf(row) === 'success');
    };

    const decorate = () => {
      list.querySelectorAll('.job').forEach((row) => {
        if (row.querySelector('.bulk-check')) return;
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.className = 'bulk-check';
        box.checked = selection.has(row.dataset.id);
        box.setAttribute('aria-label', `Chọn ${emailOf(row) || 'job'}`);
        box.addEventListener('click', (event) => event.stopPropagation());
        box.addEventListener('change', update);
        row.prepend(box);
      });
      update();
    };
    new MutationObserver(decorate).observe(list, { childList: true, subtree: true });
    decorate();

    toolbar.addEventListener('click', async (event) => {
      const select = event.target.closest('[data-bulk-select]')?.dataset.bulkSelect;
      if (select) {
        list.querySelectorAll('.job').forEach((row) => {
          const box = row.querySelector('.bulk-check');
          const status = statusOf(row);
          const plan = row.querySelector('.plan-badge')?.textContent.trim().toLowerCase() || '';
          if (select === 'invert') box.checked = !box.checked;
          else if (select === 'none') box.checked = false;
          else if (select === 'all') box.checked = true;
          else if (select === 'plus') box.checked = status === 'success' && plan !== '' && plan !== 'free';
          else if (select === 'free') box.checked = status === 'success' && (!plan || plan === 'free');
          else box.checked = status === select;
        });
        update();
        return;
      }

      const action = event.target.closest('[data-bulk-action]')?.dataset.bulkAction;
      if (!action) return;
      const rows = selectedRows(list);
      if (!rows.length) { toast('Chưa chọn job', 'warn'); return; }
      const emails = rows.map(emailOf);

      if (action === 'rotate') {
        try {
          await window.SessionRotation.enqueue(rows.filter((row) => statusOf(row) === 'success').map((row) => row.dataset.id));
        } catch (error) { toast(error.message, 'error'); }
        return;
      }
      if (config.session) await window.SessionRotation.refresh();

      if (action === 'retry' || action === 'remove') {
        let done = 0;
        rows.forEach((row) => {
          const status = statusOf(row);
          const allowed = action === 'retry'
            ? ['error', 'cancelled'].includes(status)
            : !['running', 'queued'].includes(status);
          const button = allowed && row.querySelector(`[data-action="${action === 'retry' ? 'retry' : 'remove'}"]`);
          if (button) { button.click(); done += 1; }
        });
        toast(`${done} job đã nhận lệnh; ${rows.length - done} bỏ qua`, done ? 'success' : 'warn');
        return;
      }

      if (action === 'json') {
        const results = (await Promise.all(rows.filter((row) => statusOf(row) === 'success').map((row) => fetchSession(row.dataset.id).catch(() => null)))).filter(Boolean);
        if (!results.length) { toast('Không có session JSON hợp lệ', 'warn'); return; }
        await window.GptUi.copyWithToast(JSON.stringify(results, null, 2), `Đã copy ${results.length} session JSON`);
        return;
      }

      let lines = [];
      if (config.link) lines = rows.map((row) => row.querySelector('.job-meta')?.textContent.trim()).filter(Boolean);
      else lines = linesForEmails([].concat(config.success || [], config.error || []), emails);
      const text = lines.join('\n');
      if (!text) { toast('Không có dữ liệu hợp lệ để xuất', 'warn'); return; }
      if (action === 'copy') await window.GptUi.copyWithToast(text, `Đã copy ${lines.length} dòng`);
      else { download(`bulk-${config.tab}-${Date.now()}.txt`, text); toast(`Đã export ${lines.length} dòng`); }
    });
  }

  configs.forEach(mount);
})();
