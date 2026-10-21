"""CLI standalone: python -m codex_auth --email ... --password ... [--secret ...]

Lấy Codex auth.json cho 1 account và ghi ra file.
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import CodexAuthError
from .runner import get_codex_auth_sync, write_auth_json
from .sms import create_sms_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex_auth", description="Lấy Codex OAuth auth.json")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--secret", default=None, help="TOTP base32 secret (nếu account có 2FA)")
    parser.add_argument("--proxy", default=None, help="http://user:pass@host:port")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive", action="store_true",
                        help="human-in-the-loop: tự điền pass/2FA, chờ user xử lý challenge (nên kèm headed)")
    parser.add_argument("--sms-provider", choices=["viotp", "chothuesimcode"], default=None,
                        help="Tên dịch vụ thuê số SMS (viotp | chothuesimcode)")
    parser.add_argument("--sms-key", default=None, help="API key của dịch vụ SMS")
    parser.add_argument("--sms-service-id", default=None, help="Service/App ID trên dịch vụ SMS (mặc định: ViOTP='1', Chothuesimcode='1005')")
    parser.add_argument("--force-browser", action="store_true", help="Bắt buộc dùng Camoufox browser thay vì chế độ Pure API mặc định")
    parser.add_argument("--no-api-key", action="store_true", help="bỏ token-exchange API key")
    parser.add_argument("--keep-open", action="store_true", help="giữ browser mở (debug, headed)")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--out", default="auth.json", help="đường dẫn ghi auth.json")
    args = parser.parse_args(argv)

    # Interactive cần thời gian cho human → nới timeout nếu user để mặc định.
    if args.interactive and args.timeout == 150.0:
        args.timeout = 600.0

    sms_provider = None
    if args.sms_provider and args.sms_key:
        sms_provider = create_sms_provider(
            provider_name=args.sms_provider,
            api_key=args.sms_key,
            service_id=args.sms_service_id,
        )

    try:
        auth_json = get_codex_auth_sync(
            email=args.email,
            password=args.password,
            secret=args.secret,
            proxy=args.proxy,
            headless=args.headless,
            interactive=args.interactive,
            sms_provider=sms_provider,
            use_api=not args.force_browser,
            fetch_api_key=not args.no_api_key,
            overall_timeout=args.timeout,
            keep_open=args.keep_open,
        )
    except CodexAuthError as exc:
        print(f"[codex] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    path = write_auth_json(auth_json, args.out)
    print(f"[codex] đã ghi {path}")
    print(json.dumps({**auth_json, "tokens": {**auth_json["tokens"],
          "access_token": "<redacted>", "refresh_token": "<redacted>",
          "id_token": "<redacted>"}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
