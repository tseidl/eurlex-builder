"""Standalone persistent Docling worker process."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def _send(control, message: list) -> bool:
    try:
        control.write(json.dumps(message, separators=(",", ":")) + "\n")
        control.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m eurlex_builder.extractors.docling_process LOG")

    diagnostics_path = sys.argv[1]
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["MKL_NUM_THREADS"] = "2"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "2"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    control_fd = os.dup(sys.stdout.fileno())
    with (
        os.fdopen(control_fd, "w", encoding="utf-8", buffering=1) as control,
        open(diagnostics_path, "a", encoding="utf-8", buffering=1) as diagnostics,
    ):
        sys.stdout.flush()
        os.dup2(diagnostics.fileno(), sys.stdout.fileno())
        with (
            contextlib.redirect_stdout(diagnostics),
            contextlib.redirect_stderr(diagnostics),
        ):
            try:
                import torch
                from docling.datamodel.accelerator_options import AcceleratorOptions
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import (
                    DocumentConverter,
                    PdfFormatOption,
                )
                from eurlex_builder.extractors.pdf import _conversion_result_error

                torch.set_num_threads(2)
                torch.set_num_interop_threads(1)
                options = PdfPipelineOptions(
                    accelerator_options=AcceleratorOptions(
                        num_threads=2,
                        device=os.environ.get("EURLEX_DOCLING_DEVICE", "auto"),
                    ),
                )
                converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                })
                converter.initialize_pipeline(InputFormat.PDF)
            except Exception as exc:
                _send(control, ["startup_error", f"{type(exc).__name__}: {exc}"])
                return

            if not _send(control, ["ready"]):
                return

            for line in sys.stdin:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    return
                if not message or message[0] == "shutdown":
                    return

                _, request_id, pdf_path, output_path = message
                try:
                    result = converter.convert(pdf_path)
                    result_error = _conversion_result_error(result)
                    if result_error is not None:
                        if not _send(
                            control,
                            ["result", request_id, "partial", result_error],
                        ):
                            return
                        continue
                    Path(output_path).write_text(
                        result.document.export_to_markdown(), encoding="utf-8"
                    )
                except Exception as exc:
                    if not _send(
                        control,
                        [
                            "result",
                            request_id,
                            "conversion_error",
                            f"{type(exc).__name__}: {exc}",
                        ],
                    ):
                        return
                    continue
                if not _send(control, ["result", request_id, "ok", None]):
                    return


if __name__ == "__main__":
    main()
