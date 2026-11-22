"""Prepare local integration secrets; only writes inside news_parser."""

import re
import secrets
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser_env = Path(".env")
    if not parser_env.exists():
        raise SystemExit("Create news_parser/.env from .env.example first")
    destination = Path(".scratch/runtime/integration.env")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text(
            f"FILE_DB_PASSWORD={secrets.token_hex(24)}\nPARSER_UPLOAD_TOKEN={secrets.token_hex(32)}\n",
            encoding="utf-8",
        )
    token = dotenv_values(destination)["PARSER_UPLOAD_TOKEN"]
    content = parser_env.read_text(encoding="utf-8")
    if "FILE_SERVICE_TOKEN=" in content:
        content = re.sub(
            r"^FILE_SERVICE_TOKEN=.*$", "FILE_SERVICE_TOKEN=" + token, content, flags=re.M
        )
    else:
        content += "\nFILE_SERVICE_TOKEN=" + token + "\n"
    parser_env.write_text(content, encoding="utf-8")
    print("Local integration credential configured. Restart the parser API and worker.")


if __name__ == "__main__":
    main()
