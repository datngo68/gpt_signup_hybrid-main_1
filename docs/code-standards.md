# Code Standards & Development Guidelines

---

## Python Conventions

### File Naming
- **Module files:** `snake_case.py` — e.g., `browser_phase.py`, `mail_providers.py`
- **Package directories:** `snake_case/` — e.g., `icloud_hme/`, `db/`, `web/`
- **Max line length:** 100–120 characters (formatted with Black or Ruff)
- **Avoid:** CamelCase for modules, generic names like `util.py`, `helper.py`, `common.py`

### Imports

```python
# Order: stdlib, third-party, local
import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx
import pydantic
from fastapi import FastAPI

from . import config
from .db import get_engine
from .mail_providers import MailProvider
```

- Use `from __future__ import annotations` (modern typing, Python 3.11+)
- No wildcard imports (`from x import *`)
- Prefer absolute imports over relative in large packages

### Type Hints

```python
from typing import Optional, AsyncGenerator
from pydantic import BaseModel

class SignupRequest(BaseModel):
    email: str
    password: str
    mail_provider: str = "outlook"
    proxy: Optional[str] = None

async def run_signup(request: SignupRequest) -> dict[str, Any]:
    """Signup orchestrator.
    
    Args:
        request: Signup parameters.
    
    Returns:
        SignupResult dict with email, user_id, two_factor.
    
    Raises:
        SentinelFailedError: PoW solver timeout.
        OtpTimeoutError: OTP not received in time.
    """
    ...
```

- Always type function args and return values
- Use modern syntax: `dict[str, Any]` instead of `Dict[str, Any]`
- Document exceptions via docstring `Raises:` section
- Use `Optional[T]` for nullable, `Union[A, B]` for multiple types

### Async Patterns

```python
async def fetch_otp(email: str) -> Optional[str]:
    """Poll mail provider for OTP."""
    timeout = 180  # seconds
    start = time.time()
    while time.time() - start < timeout:
        otp = await mail_provider.get_otp(email)
        if otp:
            return otp
        await asyncio.sleep(5)
    raise OtpTimeoutError(f"No OTP for {email} after {timeout}s")

async def run_jobs(jobs: list[Job]) -> list[Result]:
    """Run jobs with bounded concurrency."""
    sem = asyncio.Semaphore(3)  # 3 concurrent
    
    async def bounded_job(job: Job) -> Result:
        async with sem:
            return await process_job(job)
    
    return await asyncio.gather(*[bounded_job(j) for j in jobs])
```

- Use `asyncio.Semaphore` for concurrency limits (not ThreadPool)
- Avoid blocking calls in async functions (use `loop.run_in_executor()` if needed)
- Use `asyncio.sleep()` not `time.sleep()` in async code
- Wrap coroutines in `asyncio.gather()` or `asyncio.create_task()` for parallel execution

### Error Handling

```python
# Define custom exceptions near module top
class SentinelFailedError(Exception):
    """Sentinel PoW solver timeout or failure."""
    pass

class OtpTimeoutError(Exception):
    """OTP not received within timeout window."""
    pass

# Use them
try:
    otp = await fetch_otp(email)
except OtpTimeoutError as e:
    logger.error(f"OTP fetch failed for {email}: {e}")
    raise  # re-raise after logging

# Avoid naked except:
# ❌ except: pass
# ✅ except SomeSpecificError: handle_gracefully()
# ✅ except Exception as e: logger.exception(f"Unexpected error: {e}")
```

- Define custom exceptions per module (cluster at top)
- Log exceptions with context (email, attempt count, etc.)
- Never silently swallow exceptions without logging
- Use specific exception types, not generic `Exception`

### Logging

```python
import logging

logger = logging.getLogger(__name__)

logger.info(f"Starting signup for {email}")
logger.debug(f"Using proxy {proxy}")
logger.warning(f"OTP retry attempt {attempt}/{max_retries}")
logger.error(f"Sentinel PoW failed: {err}", exc_info=True)
```

- Use `logging` module, not `print()`
- Log at appropriate levels: INFO (milestones), DEBUG (details), WARNING (recoverable), ERROR (exceptions)
- Include context (email, job_id, attempt) in messages
- Use `exc_info=True` or `.exception()` when logging caught exceptions

### Naming Conventions

```python
# Constants: UPPER_SNAKE_CASE
OUTLOOK_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
_SENTINEL_QUICKJS_PATH = "openai_sentinel_quickjs.js"  # private

# Functions & methods: lower_snake_case
def run_signup(request: SignupRequest) -> SignupResult:
    pass

async def fetch_otp_from_worker(email: str) -> str:
    pass

# Classes: PascalCase
class MailProvider(Protocol):
    async def get_otp(self, email: str) -> Optional[str]:
        ...

class OutlookMailProvider(MailProvider):
    pass

# Private: prefix with _
def _internal_helper() -> None:
    """Not part of public API."""
    pass

_PRIVATE_CONSTANT = "hidden"
```

- Constants: `UPPER_SNAKE_CASE` (module-level, no lowercase)
- Private functions/variables: prefix `_` (convention, not enforced)
- Methods: `lower_snake_case`
- Classes: `PascalCase`

### Docstrings

```python
def run_signup(request: SignupRequest) -> SignupResult:
    """Orchestrate ChatGPT account signup (hybrid flow).
    
    Executes browser phase (Camoufox + Playwright) or pure HTTP phase
    (curl_cffi), polls OTP via mail provider, then extracts session
    tokens. Optionally enrolls TOTP 2FA.
    
    Args:
        request: SignupRequest with email, password, mail_provider, etc.
    
    Returns:
        SignupResult with user_id, session_token, access_token,
        and optional two_factor dict.
    
    Raises:
        SentinelFailedError: PoW solver timeout or max retries exceeded.
        OtpTimeoutError: OTP not received after 180s polling.
        BrowserPhaseError: Camoufox automation failed.
        HTTPPhaseError: Token extraction failed.
    
    Note:
        Timeout is HYBRID_JOB_TIMEOUT env var (default 240s),
        must be > 180s (OTP timeout). Browser mode is headed by default
        (higher anti-detect; headless not recommended).
    """
```

- **One-liner:** brief summary (fits in IDE docstring popup)
- **Multi-line:** full description, Args, Returns, Raises, Note/Warning
- **Args:** type + description per arg
- **Returns:** type + description of return value
- **Raises:** list exceptions that can be raised + reason
- **Note/Warning:** special constraints or caveats

### Code Style

```python
# Line length: target 100–120 chars (Black default 88)
# Break long lines with backslash or parentheses
result = run_signup(
    email=email,
    password=password,
    mail_provider=mail_provider,
    proxy=proxy,
)

# Spaces around operators
x = 1 + 2  # ✓
x=1+2      # ✗

# List comprehensions OK (keep simple)
emails = [combo.email for combo in combos if not combo.is_used]

# f-strings preferred
message = f"User {email} signed up at {created_at}"

# Dictionary unpacking
config = {**default_config, **user_config}

# Use walrus operator (Python 3.8+) sparingly
if (match := pattern.search(text)):
    value = match.group(1)
```

- Follow Black formatting (if linting enabled)
- Spaces around operators (`=`, `+`, etc.)
- 4-space indentation (stdlib, not tabs)
- Blank lines: 2 between top-level functions, 1 between methods

---

## Pydantic Models

```python
from pydantic import BaseModel, Field, validator
from typing import Optional

class SignupRequest(BaseModel):
    """Signup job parameters."""
    
    email: str = Field(..., description="Email address")
    password: str = Field(..., min_length=8)
    mail_provider: str = Field(default="outlook", description="OTP backend")
    account_type: str = Field(default="free", pattern="^(free|plus)$")
    proxy: Optional[str] = None
    
    @validator("email")
    def email_valid(cls, v):
        if "@" not in v:
            raise ValueError("Invalid email")
        return v.lower()
    
    class Config:
        frozen = False  # Allow mutations (or True if immutable)
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "secure123",
                "mail_provider": "outlook",
            }
        }
```

- Use Pydantic for API request/response validation
- Document fields with `Field(description=...)`
- Add validators for business logic (email format, length, patterns)
- Use `Optional[T]` with defaults for nullable fields
- Provide JSON schema examples in `Config.json_schema_extra`

---

## Database & SQLite

### Schema Additions

```python
# In db/schema.py, add DDL for new table
CREATE_MY_TABLE = """
    CREATE TABLE IF NOT EXISTS my_table (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
"""

# In db/migrate.py, increment version and add idempotent migration
CURRENT_VERSION = 5

MIGRATIONS = {
    1: [CREATE_JOBS, CREATE_JOB_LOGS, ...],
    2: [CREATE_OUTLOOK_COMBOS, ...],
    3: [ALTER_JOBS_ADD_COLUMN],
    4: [CREATE_SETTINGS],
    5: [CREATE_MY_TABLE],  # New version
}

# Migration must be idempotent (safe to run multiple times)
CREATE_MY_TABLE = """
    CREATE TABLE IF NOT EXISTS my_table (...)  -- IF NOT EXISTS
"""
```

- Use `IF NOT EXISTS` for safe re-runs
- Always increment `CURRENT_VERSION` monotonically
- Test migrations on fresh DB and existing DB with data
- Backup `runtime/data.db` before applying new migrations

### Repository Pattern

```python
class MyTableRepository:
    """Data access for my_table."""
    
    def __init__(self, engine: DatabaseEngine):
        self.engine = engine
    
    def insert(self, email: str, status: str = "pending") -> int:
        """Insert row, return row id."""
        with self.engine.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO my_table (email, status) VALUES (?, ?)",
                (email, status)
            )
            return cur.lastrowid
    
    def get_by_email(self, email: str) -> Optional[dict]:
        """Fetch row by email."""
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM my_table WHERE email = ?",
                (email,)
            ).fetchone()
            return dict(row) if row else None
    
    def update_status(self, email: str, new_status: str) -> int:
        """Update status, return rows affected."""
        with self.engine.transaction() as conn:
            cur = conn.execute(
                "UPDATE my_table SET status = ? WHERE email = ?",
                (new_status, email)
            )
            return cur.rowcount
```

- Use **transaction context** (`self.engine.transaction()`)
- Return **simple types** (int, dict, list) not ORM objects
- Use **parameterized queries** (`?` placeholders, never f-string SQL)
- Prefix private methods with `_`

---

## FastAPI Routes

```python
from fastapi import APIRouter, HTTPException, Depends
from typing import Optional

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

@router.post("/register", response_model=JobResponse)
async def register_job(
    request: SignupRequest,
    token: str = Depends(verify_token),
) -> JobResponse:
    """Register a signup job.
    
    Args:
        request: Signup parameters.
        token: Auth token (verified via dependency injection).
    
    Returns:
        Job ID and initial status.
    
    Raises:
        HTTPException(401): Token invalid or missing.
        HTTPException(400): Request validation failed.
    """
    try:
        job_id = await manager.enqueue(request)
        return JobResponse(id=job_id, status="pending")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Job registration failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error")

@router.get("/{job_id}", response_model=JobStatus)
async def get_job_status(
    job_id: int,
    token: str = Depends(verify_token),
) -> JobStatus:
    """Get job status and progress."""
    job = await manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatus(**job)
```

- Use **APIRouter** for modular routes (group by resource type)
- Document endpoints with docstrings (OpenAPI auto-generates)
- Use **dependency injection** for auth (`Depends(verify_token)`)
- **Validate inputs** via Pydantic request models
- **Raise HTTPException** for API errors (set appropriate status codes)
- **Log exceptions** before returning 5xx errors

---

## Web Frontend (Static JS)

```javascript
// File: web/static/app.js
// No bundler; vanilla ES6 (no imports/exports within browser)

const API_BASE = "/api";
const SSE_PATH = "/api/sse";

class JobManager {
  constructor(token) {
    this.token = token;
    this.jobs = new Map();
  }

  async submitJob(request) {
    const response = await fetch(`${API_BASE}/jobs/register`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Token": this.token,
      },
      body: JSON.stringify(request),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  listenSSE() {
    const eventSource = new EventSource(`${SSE_PATH}?token=${this.token}`);
    eventSource.addEventListener("job_updated", (e) => {
      const data = JSON.parse(e.data);
      this.jobs.set(data.id, data);
      this.updateUI();
    });
    return eventSource;
  }

  updateUI() {
    // Update DOM based on this.jobs
    const container = document.getElementById("job-list");
    container.innerHTML = Array.from(this.jobs.values())
      .map(job => `<div>${job.email}: ${job.status}</div>`)
      .join("");
  }
}

// Usage
const manager = new JobManager(getAuthToken());
manager.listenSSE();
```

- **No framework** (vanilla JS only)
- **No bundler** (inline or simple `<script>` tags)
- **Fetch API** for HTTP (modern, Promise-based)
- **EventSource** for SSE (subscribe to live events)
- **DOM manipulation** via `document.getElementById()`, `innerHTML`, `classList`
- **Modular classes** (JobManager, Settings, etc.) for organization

---

## Git Commit Messages

```bash
# Format: <type>: <subject>
# Types: feat, fix, docs, refactor, test, chore, perf, ci, build

# ✓ Good
git commit -m "feat: add iCloud HME bulk deactivate action"
git commit -m "fix: Outlook refresh token rotation on Graph API call"
git commit -m "docs: update codebase summary with new architecture"

# ✗ Avoid
git commit -m "updated code"
git commit -m "WIP: test something"
git commit -m "merge main"

# Multi-line (for complex changes)
git commit -m "refactor: split browser_phase into state machine + handlers

- Extract page-state logic into separate module
- Reduce cyclomatic complexity of _drive_signup_flow
- Add unit tests for state transitions

Closes #42"
```

- **Type:** feat (new feature), fix (bug fix), docs, refactor, test, perf, ci, chore
- **Subject:** lowercase, imperative, ~50 chars max
- **Body:** explain why, not what (git show diff for what)
- **Reference issues:** "Closes #123" or "Relates to #456"
- **No AI references:** never say "Claude", "AI-generated", etc.

---

## Testing Principles

### Current State
- Limited automated test suite (under `test/`)
- Verification scripts (`check_*.py`, `smoke_*.py`) for sanity
- Manual CLI/UI testing for signup flows

### Adding Tests

```python
# In test/test_mail_providers.py
import pytest
from gpt_signup_hybrid.mail_providers import OutlookMailProvider

@pytest.mark.asyncio
async def test_outlook_refresh_token():
    """Test Outlook Graph API token refresh."""
    provider = OutlookMailProvider(
        email="test@outlook.com",
        password="test123",
        refresh_token="old_token",
        client_id="test_client",
    )
    # Mock Graph API response
    # Assert token updated before API call
    # Assert token persisted to SQLite
    pass

@pytest.mark.asyncio
async def test_otp_timeout():
    """Test OTP polling timeout."""
    provider = OutlookMailProvider(...)
    # Mock mail API to return None
    # Assert OtpTimeoutError raised after 180s
    pass
```

- Use `pytest` for unit tests
- Use `pytest-asyncio` for async tests
- Mock external APIs (don't hit real services in tests)
- Test error paths, not just happy path
- Aim for >80% coverage on critical modules

---

## Security Best Practices

### Credential Handling

```python
# ✓ Good: persist before mutation, log redacted
original_token = combo.refresh_token
try:
    combo.refresh_token = await graph_api.refresh(original_token)
    combo_repo.update(combo)  # Persist immediately
except Exception as e:
    logger.error(f"Refresh failed for {combo.email} [token redacted]")
    raise

# ✗ Bad: mutate then fail to persist
combo.refresh_token = new_token
await graph_api.call(new_token)  # Fails, old token lost
```

- Always **persist before mutation**
- **Redact credentials in logs** (use helper functions)
- **Timeout on external API calls** (prevent hanging)
- **Validate inputs** via Pydantic (reject unexpected types)

### Secrets in Code

```python
# ✗ Never hardcode
API_KEY = "sk-123456"

# ✓ Load from environment
import os
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("OPENAI_API_KEY not set")

# ✓ Or use pydantic-settings
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str
    
    class Config:
        env_file = ".env"
```

- Load from `.env` or environment variables
- **Never commit `.env` files** (add to `.gitignore`)
- **Rotate tokens periodically**
- **Scope tokens** to minimal permissions

---

## Performance Guidelines

### Concurrency

```python
# ✓ Use Semaphore for bounded concurrency
sem = asyncio.Semaphore(5)

async def process_job(job):
    async with sem:
        return await do_work(job)

tasks = [process_job(j) for j in jobs]
results = await asyncio.gather(*tasks)

# ✗ Avoid: ThreadPool (slower for I/O-bound, overhead)
# ✗ Avoid: Unbounded asyncio.create_task() (resource exhaustion)
```

### Database Access

```python
# ✓ Use transaction context (connection pooling)
with engine.transaction() as conn:
    row = conn.execute(...).fetchone()

# ✗ Avoid: new connection per query
conn = sqlite3.connect(db_path)
row = conn.execute(...).fetchone()
conn.close()  # Expensive
```

### HTTP Caching

```python
# ✓ Cache Stripe js_checksum (24h TTL)
STRIPE_BUNDLE_CACHE = {}  # {sha256(bundle): {js_checksum, rv_timestamp}}

async def extract_config_cached(bundle_url):
    sha = hashlib.sha256(await fetch_bundle(bundle_url)).hexdigest()
    if sha in STRIPE_BUNDLE_CACHE:
        return STRIPE_BUNDLE_CACHE[sha]
    result = extract_config_live(bundle_url)
    STRIPE_BUNDLE_CACHE[sha] = result
    return result
```

- **Cache expensive operations** (Stripe bundle, sentinel PoW)
- **Use connection pooling** (SQLite WAL, httpx AsyncClient)
- **Profile before optimizing** (measure actual bottleneck)

---

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| **Blocking call in async function** | Use `loop.run_in_executor()` or `asyncio.to_thread()` |
| **Nested transactions** | SQLite auto-handles, but avoid explicit nesting |
| **ORM instead of raw SQL** | Keep as raw SQL (simpler, faster for this codebase) |
| **Token refresh after failure** | Persist immediately BEFORE API call |
| **Log secrets** | Always redact credentials (email domain OK, token not) |
| **Hardcoded paths** | Use `config.RUNTIME_DIR` or `Path.cwd()` |
| **Except: pass** | Log at minimum, don't silently swallow |
| **Unbounded concurrency** | Use Semaphore, cap workers |
| **No timeout on HTTP calls** | Always set `timeout=30` or higher |
| **String concatenation in SQL** | Always use `?` placeholders |

---

## Code Review Checklist

Before submitting a PR:

- [ ] Type hints on all functions (args + return)
- [ ] Docstring on public functions (Args, Returns, Raises)
- [ ] No hardcoded secrets or API keys
- [ ] Credentials not logged (check for email addresses too)
- [ ] Error handling (try/except with logging)
- [ ] Tests added for new logic (or manual test documented)
- [ ] No breaking changes to DB schema (or migration added)
- [ ] Imports sorted (stdlib, third-party, local)
- [ ] Line length <120 characters
- [ ] No wildcard imports
- [ ] Async code uses `await` correctly
- [ ] No blocking calls in async functions
- [ ] Git commit message follows conventional format
- [ ] README/docs updated if user-facing changes
