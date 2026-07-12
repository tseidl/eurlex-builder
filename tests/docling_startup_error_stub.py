"""Worker fixture that fails during its startup handshake."""

from __future__ import annotations

import json
import sys


def main() -> None:
    sys.stdout.write(json.dumps(["startup_error", "model unavailable"]) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
