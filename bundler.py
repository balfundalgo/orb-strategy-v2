"""
bundler.py — Merges all .py files into bundled_main.py for PyInstaller.
Balfund Trading Private Limited
"""
from pathlib import Path

FILES_IN_ORDER = [
    "fyers_connect.py",
    "strategy.py",
    "gui.py",          # LAST — its __main__ block is kept
]

STRIP_PREFIXES = (
    "from fyers_connect import",
    "from strategy import",
    "from __future__ import annotations",
)

OUTPUT_FILE = "bundled_main.py"


def strip_main_block(lines):
    result = []
    in_main = False
    for line in lines:
        stripped = line.rstrip()
        if stripped in ('if __name__ == "__main__":', "if __name__ == '__main__':"):
            in_main = True
            continue
        if in_main:
            if stripped == "" or line.startswith("    ") or line.startswith("\t"):
                continue
            else:
                in_main = False
        result.append(line)
    return result


def bundle():
    output_lines = []
    output_lines.append("from __future__ import annotations\n\n")
    output_lines.append('"""\nORB Strategy V2 — Bundled Build\nBalfund Trading Private Limited\n"""\n\n')

    seen_imports = set()
    last_file = FILES_IN_ORDER[-1]

    for fname in FILES_IN_ORDER:
        path = Path(fname)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {fname}")

        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

        if fname != last_file:
            lines = strip_main_block(lines)

        output_lines.append(f"\n# {'='*70}\n# SOURCE: {fname}\n# {'='*70}\n\n")

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("from __future__"):
                continue
            if any(stripped.startswith(p) for p in STRIP_PREFIXES):
                continue
            if stripped.startswith(("import ", "from ")) and stripped in seen_imports:
                continue
            if stripped.startswith(("import ", "from ")):
                seen_imports.add(stripped)
            output_lines.append(line)

    Path(OUTPUT_FILE).write_text("".join(output_lines), encoding="utf-8")
    size = Path(OUTPUT_FILE).stat().st_size
    print(f"[BUNDLER] OK - Created {OUTPUT_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    bundle()
