"""Parse AST file web/upi_runner.py để verify cú pháp sau khi sửa _RotatingSession."""
import ast
import pathlib
import sys

TARGET = pathlib.Path(__file__).resolve().parents[1] / "web" / "upi_runner.py"


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"[FAIL] syntax — {TARGET.name} :: {exc}", flush=True)
        return 1
    print(f"[PASS] syntax — {TARGET.name} :: {len(src.splitlines())} dòng OK", flush=True)

    # Xác nhận các method mới/đã sửa tồn tại trong class _RotatingSession.
    wanted = {"_reset_to_primary", "_recover", "_call_with_retry"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_RotatingSession":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(item.name)
    missing = wanted - found
    if missing:
        print(f"[FAIL] methods — thiếu {sorted(missing)}", flush=True)
        return 1
    print(f"[PASS] methods — _RotatingSession có đủ {sorted(wanted)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
