/* Ver số: credentials stay in the input/request, never browser storage. */
(() => {
  const input = document.getElementById('codex-combos');
  const concurrency = document.getElementById('codex-concurrency');
  const message = document.getElementById('codex-message');
  const list = document.getElementById('codex-jobs');
  let polling = false;
  let jobs = [];
  const lines = () => input.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const count = () => { document.getElementById('codex-count').textContent = `${lines().length} tài khoản`; };
  input.addEventListener('input', count);
  document.getElementById('codex-import').onclick = () => document.getElementById('codex-file').click();
  document.getElementById('codex-file').onchange = async event => {
    const file = event.target.files[0];
    try {
      if (!file) return;
      if (file.size > 200000) throw new Error('Tệp TXT tối đa 200 KB');
      const text = await file.text();
      if (text.includes('\u0000')) throw new Error('Tệp không phải văn bản TXT');
      if (input.value.length + text.length > 200000) throw new Error('Tổng nội dung tối đa 200 KB');
      input.value = [input.value.trim(), text.trim()].filter(Boolean).join('\n');
      count();
      message.textContent = 'Đã nhập TXT cục bộ; chưa gửi tài khoản';
    } catch (error) { message.textContent = error.message; }
    finally { event.target.value = ''; }
  };
  document.getElementById('codex-dedup').onclick = () => {
    const before = lines(); input.value = [...new Set(before)].join('\n'); count();
    message.textContent = `Đã bỏ ${before.length - lines().length} dòng trùng; giữ biến thể mật khẩu`;
  };
  document.getElementById('codex-clear-input').onclick = () => { input.value = ''; count(); };
  async function saveDownload(path, filename) {
    const response = await fetch('/api/codex/tab' + path, { headers: { 'X-API-Token': token() }, cache: 'no-store' });
    if (!response.ok) throw new Error('Tải xuống thất bại');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  document.getElementById('codex-zip').onclick = async () => {
    try { await saveDownload('/auth.zip', 'codex-auth.zip'); } catch (error) { message.textContent = error.message; }
  };
  document.getElementById('codex-download-9router').onclick = async () => {
    try {
      await saveDownload('/9router.json', 'codex-9router.json');
      message.textContent = 'Đã tải JSON 9Router. Cảnh báo: tệp chứa token bí mật.';
    } catch (error) { message.textContent = error.message; }
  };
  document.getElementById('codex-copy-9router').onclick = async () => {
    try {
      const response = await fetch('/api/codex/tab/9router.json', { headers: { 'X-API-Token': token() }, cache: 'no-store' });
      if (!response.ok) throw new Error('Không thể lấy JSON 9Router (HTTP ' + response.status + ')');
      const secretJson = await response.text();
      try { await navigator.clipboard.writeText(secretJson); }
      catch (_) { throw new Error('Không thể ghi clipboard; token chưa được sao chép'); }
      message.textContent = 'Đã sao chép JSON 9Router. Cảnh báo: clipboard chứa token bí mật.';
    } catch (error) { message.textContent = error.message; }
  };
  for (const [id, status] of [['codex-copy-success', 'success'], ['codex-copy-error', 'error']]) {
    document.getElementById(id).onclick = async () => {
      try { await window.GptUi.copyText([...new Set(jobs.filter(job => job.status === status).map(job => job.email))].join('\n')); message.textContent = 'Đã copy email (không có mật khẩu)'; }
      catch (_) { message.textContent = 'Không thể copy email'; }
    };
  }
  document.getElementById('codex-clear-jobs').onclick = async () => {
    try { render(await api('/clear', {})); } catch (error) { message.textContent = error.message; }
  };
  const token = () => window.GptUi.getAuthToken();
  async function api(path, body) {
    const response = await fetch('/api/codex/tab' + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-API-Token': token(), 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('Yêu cầu thất bại (HTTP ' + response.status + ')');
    return response.json();
  }
  function render(snapshot) {
    jobs = snapshot.jobs;
    const exportReady = jobs.some(job => job.status === 'success' && job.download_ready);
    document.getElementById('codex-zip').disabled = !exportReady;
    document.getElementById('codex-copy-9router').disabled = !exportReady || snapshot.running;
    document.getElementById('codex-download-9router').disabled = !exportReady || snapshot.running;
    document.getElementById('codex-copy-success').disabled = !jobs.some(job => job.status === 'success');
    document.getElementById('codex-copy-error').disabled = !jobs.some(job => job.status === 'error');
    document.getElementById('codex-clear-jobs').disabled = snapshot.running || !jobs.length || jobs.some(job => ['queued', 'running', 'stopping'].includes(job.status));
    list.replaceChildren();
    document.getElementById('codex-start').disabled = snapshot.running;
    document.getElementById('codex-stop').disabled = !snapshot.running;
    document.getElementById('codex-summary').textContent = `${snapshot.jobs.length} công việc`;
    if (!snapshot.jobs.length) {
      const empty = document.createElement('p');
      empty.className = 'codex-empty muted';
      empty.textContent = 'Chưa có công việc. Nhập tài khoản rồi nhấn Chạy.';
      list.append(empty);
    }
    const statuses = { queued: 'Đang chờ', running: 'Đang chạy', stopping: 'Đang dừng', stopped: 'Đã dừng', success: 'Hoàn tất', error: 'Lỗi' };
    const labels = { unknown: 'Chưa xác định', required: 'Cần xác minh', verified: 'Đã xác minh số', not_required: 'Không yêu cầu thêm số' };
    for (const job of snapshot.jobs) {
      const row = document.createElement('section');
      row.className = 'card';
      const title = document.createElement('p');
      title.textContent = `${job.email} — ${statuses[job.status] || job.status} — ${labels[job.phone_status]}`;
      row.append(title);
      const logs = document.createElement('pre');
      logs.textContent = job.logs.join('\n');
      row.append(logs);
      if (job.download_ready) {
        const download = document.createElement('button');
        download.className = 'btn btn-ghost';
        download.textContent = 'Tải auth.json';
        download.onclick = async () => {
          try {
            const response = await fetch(`/api/codex/tab/jobs/${job.id}/auth.json`, { headers: { 'X-API-Token': token() }, cache: 'no-store' });
            if (!response.ok) throw new Error('Download thất bại');
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url; link.download = 'auth.json'; link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (error) { message.textContent = error.message; }
        };
        row.append(download);
      }
      list.append(row);
    }
  }
  async function refresh() {
    if (polling || !document.getElementById('tab-codex').classList.contains('active')) return;
    polling = true;
    try {
      render(await api('/jobs'));
      if (message.dataset.connectionError) { message.textContent = 'Đã kết nối'; delete message.dataset.connectionError; }
    } catch (error) {
      if (document.getElementById('tab-codex').classList.contains('active')) {
        message.textContent = error.message.includes('404') ? 'Backend chưa có Ver số. Cần khởi động lại đúng server dự án.' : error.message;
        message.dataset.connectionError = '1';
      }
    }
    finally { polling = false; }
  }
  document.getElementById('codex-start').onclick = async () => {
    const value = Number(concurrency.value);
    if (!Number.isInteger(value) || value < 1 || value > 10) {
      message.textContent = 'Đồng thời phải là số nguyên 1..10'; return;
    }
    try {
      await Settings.save('codex.concurrency', value, token());
      render(await api('/start', { combos: input.value, concurrency: value }));
      input.value = ''; count();
      message.textContent = 'Đã bắt đầu';
    } catch (error) { message.textContent = error.message; }
  };
  document.getElementById('codex-stop').onclick = async () => {
    try { render(await api('/stop', {})); message.textContent = 'Đang dừng; chờ request đang chạy hoàn tất'; }
    catch (error) { message.textContent = error.message; }
  };
  document.addEventListener('gpt:tab', event => {
    if (event.detail.tab === 'codex') {
      concurrency.value = Settings.get('codex.concurrency') ?? 1;
      refresh();
    }
  });
  setInterval(() => {
    if (document.getElementById('tab-codex').classList.contains('active')) refresh();
  }, 1500);
})();
