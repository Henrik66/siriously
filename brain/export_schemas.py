"""Write the contract JSON Schemas to ./schemas/ (for the Mac side): python -m brain.export_schemas."""

from __future__ import annotations

import json
from pathlib import Path

from brain.contracts import SCHEMAS


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "schemas"
    out.mkdir(exist_ok=True)
    for name, schema in SCHEMAS.items():
        path = out / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
