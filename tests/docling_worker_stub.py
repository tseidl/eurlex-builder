"""Docling-free subprocess fixture for worker lifecycle tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def send(message: list) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    send(["ready"])
    for line in sys.stdin:
        message = json.loads(line)
        if message[0] == "shutdown":
            return
        _, request_id, pdf_path, output_path = message
        mode = Path(pdf_path).read_bytes()
        if b"HANG" in mode:
            Path(f"{pdf_path}.started").write_text("started", encoding="utf-8")
            time.sleep(60)
        elif b"GARBAGE_LINE" in mode:
            os.write(sys.stdout.fileno(), b"not-json\n")
            time.sleep(60)
        elif b"PARTIAL_LINE" in mode:
            os.write(sys.stdout.fileno(), b"not-json-without-newline")
            time.sleep(60)
        elif b"CRASH" in mode:
            os._exit(17)
        elif b"PARTIAL" in mode:
            send(["result", request_id, "partial", "page failed"])
        elif b"LARGE" in mode:
            Path(output_path).write_text("x" * (8 * 1024 * 1024), encoding="utf-8")
            send(["result", request_id, "ok", None])
        else:
            Path(output_path).write_text("Article 1\nStub text", encoding="utf-8")
            send(["result", request_id, "ok", None])


if __name__ == "__main__":
    main()
