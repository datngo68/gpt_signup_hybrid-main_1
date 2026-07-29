# syntax=docker/dockerfile:1
#
# gpt_signup_hybrid — production image (target Linux/amd64).
# Camoufox chạy headless qua Xvfb (xvfb-run trong CMD).
# Multi-stage: builder (fat, compiler + browser fetch) → runtime (slim).
#
# ⚠ Build trên Apple Silicon ra image arm64 — KHÔNG chạy được trên VPS amd64.
#   Deploy amd64: docker buildx build --platform linux/amd64 -t gsh:latest .
#   (build trực tiếp trên VPS amd64 thì docker build bình thường là đủ.)

# ===========================================================================
# Stage 1: builder — cài deps, pre-bake Camoufox binary + GeoIP mmdb.
# ===========================================================================
FROM python:3.13-slim-bookworm AS builder

# HOME nhất quán giữa 2 stage để cache (~/.cache) copy sang hợp lệ.
# PLAYWRIGHT_BROWSERS_PATH ghim cache playwright dưới HOME (deterministic).
ENV HOME=/home/appuser \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright
RUN useradd -m -u 10001 appuser

# build-essential cho Cython/lxml/curl_cffi nếu thiếu wheel manylinux.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

# venv riêng (/opt/venv) — strip macOS-only deps (pyobjc: không có wheel Linux,
# pip sẽ fail). pyobjc chỉ là backend macOS của screeninfo, 0 import trực tiếp.
# Giữ requirements.txt gốc nguyên (macOS dev vẫn dùng).
RUN python -m venv /opt/venv \
    && . /opt/venv/bin/activate \
    && pip install --upgrade pip \
    && grep -ivE '^(pyobjc-core|pyobjc-framework-Cocoa)==' requirements.txt > requirements.linux.txt \
    && pip install -r requirements.linux.txt
ENV PATH=/opt/venv/bin:$PATH

# Pre-bake browser binary + GeoIP mmdb vào $HOME/.cache → runtime offline,
# không download lúc launch. 'camoufox fetch' là public CLI documented.
# Fallback download_mmdb() mirror đúng call của browser_phase.py (idempotent
# nếu fetch đã kéo mmdb).
RUN playwright install firefox \
    && python -m camoufox fetch \
    && python -c "from camoufox.locale import MMDB_FILE, download_mmdb; MMDB_FILE.exists() or download_mmdb()"

# ===========================================================================
# Stage 2: runtime — slim, chỉ deps chạy + Xvfb + FF runtime libs.
# ===========================================================================
FROM python:3.13-slim-bookworm AS runtime

# RUNTIME_DIR absolute → persistence (data.db, sessions) bám đúng volume mount
# bất kể WORKDIR/working_dir, không phụ thuộc cwd-relative resolve.
ENV HOME=/home/appuser \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright \
    RUNTIME_DIR=/app/gpt_signup_hybrid/runtime
RUN useradd -m -u 10001 appuser

# xvfb (headless display) + xauth (xvfb-run BẮT BUỘC) + curl (healthcheck)
# + tini (init/PID 1: xvfb-run kẹt ở Xvfb-readiness khi chạy làm PID 1 — cần
#   init thật reap/forward signal để xvfb-run launch được web process).
# + nodejs: sentinel_quickjs chạy OpenAI sdk.js trong Node subprocess để build
#   sentinel token vượt deep server-side verification. Thiếu node → fallback
#   Python PoW (token yếu, /password/verify có thể reject với invalid_state).
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth curl tini nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# FF runtime shared libs (libgtk, libX11, libdbus, libasound...) cho
# Camoufox/Playwright Firefox. install-deps chỉ apt, không tải browser.
RUN playwright install-deps firefox && rm -rf /var/lib/apt/lists/*

# Camoufox binary + GeoIP mmdb + playwright cache (đã pre-bake ở builder).
COPY --from=builder --chown=appuser:appuser /home/appuser/.cache /home/appuser/.cache

# Source → /app/gpt_signup_hybrid/ ; PYTHONPATH=/app → import gpt_signup_hybrid.
# Repo root có __init__.py + __main__.py → package resolve trực tiếp (no shim).
COPY --chown=appuser:appuser . /app/gpt_signup_hybrid/
COPY --chown=appuser:appuser docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Pre-create runtime/ owned by appuser. BẮT BUỘC: named volume mount đè lên
# path này; nếu path vắng trong image, Docker tạo volume root-owned →
# appuser không ghi được → migrate crash. Tạo sẵn (appuser) để volume rỗng
# kế thừa ownership appuser.
RUN mkdir -p /app/gpt_signup_hybrid/runtime \
    && chown appuser:appuser /app/gpt_signup_hybrid/runtime

WORKDIR /app/gpt_signup_hybrid
USER appuser

# tini làm PID 1 (init thật) → entrypoint/xvfb-run chạy làm child.
# Entrypoint chạy migrate (idempotent) rồi exec CMD.
# --unsafe-expose-network: web guard từ chối bind non-loopback (0.0.0.0) nếu
# không opt-in. Trong container BẮT BUỘC bind 0.0.0.0 (publish ra host loopback
# 127.0.0.1 + token auth + reverse-proxy ngoài → an toàn).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["xvfb-run", "-a", "python", "-m", "gpt_signup_hybrid", "web", "--host", "0.0.0.0", "--port", "8083", "--unsafe-expose-network"]
