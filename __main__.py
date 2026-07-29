"""python -m gpt_signup_hybrid → CLI."""
from _expire_check import enforce_expiry

# Enforce expire TRƯỚC khi import CLI / FastAPI / Playwright — block sớm
# trong các build expired để user thấy thông báo ngay, không phải đợi
# uvicorn boot xong.
enforce_expiry()

from cli import app  # noqa: E402


if __name__ == "__main__":
    app()
