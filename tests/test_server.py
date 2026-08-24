from __future__ import annotations

import logging
from pathlib import Path

from strava_mcp.config import Settings
from strava_mcp.server import configure_logging


def test_logging_is_configured_for_stderr_and_not_stdout(tmp_path: Path, capsys) -> None:
    configure_logging(Settings(config_dir=tmp_path, openapi_path=tmp_path / "openapi.json"))
    logging.getLogger("test-server").warning("diagnostic")
    captured = capsys.readouterr()
    assert captured.out == ""
