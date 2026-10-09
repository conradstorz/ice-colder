"""Dispenser-profile loading and wiring used during startup."""

import sys

from loguru import logger

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.dispensers import (
    DispenserProfiles,
    Finding,
    ValidationReport,
    dispensers_path,
)
from web_interface import routes


def load_dispenser_profiles(config: ConfigModel) -> DispenserProfiles:
    """
    Load `dispensers.toml` (path from `ICE_COLDER_DISPENSERS`, default
    `dispensers.toml`) against `config`'s product catalog and log the
    resulting `ValidationReport`: each finding at `warning` or `error`
    per its own severity, then the report's verdict line at `info`.

    A missing file is a single warning finding (dispensers.py already
    turns it into one) — logged, not fatal; the VMC's own CFG-101/CFG-102
    reconciliation handles product gating. A directory at that path mirrors
    `load_config`'s own directory check: log a clear error and
    `sys.exit(1)` rather than paper over it.

    Extracted from `main()` so it can be exercised in a test without an
    event loop.
    """
    path = dispensers_path()
    logger.info(f"Loading dispenser profiles from '{path}'")

    profiles = DispenserProfiles(config, path=path)
    try:
        report = profiles.load()
    except IsADirectoryError:
        logger.error(
            f"Dispensers path '{path}' is a directory, not a file. This "
            "typically happens when a Docker bind-mount targets a file "
            "path that doesn't exist yet on the host, so Docker creates a "
            "directory there instead. Remove the directory and fix the "
            "bind-mount/ICE_COLDER_DISPENSERS setting, then retry."
        )
        sys.exit(1)
    except Exception as exc:
        # Anything else out of load() (a pathological TOML file blowing
        # the recursion limit, an unreadable file slipping past
        # DispenserProfiles' own OSError handling, ...) must never crash
        # startup -- a bad dispensers.toml should cost dispenser profiles,
        # never the whole machine.
        logger.error(f"dispensers.toml could not be loaded: {exc}")
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        profiles.report = ValidationReport(
            findings=[
                Finding(
                    slot=None,
                    path="",
                    line=None,
                    severity="error",
                    message=f"dispensers.toml could not be loaded: {first_line}",
                )
            ],
            file_error=True,
        )
        return profiles

    for finding in report.findings:
        prefix = "File" if finding.slot is None else f"Slot {finding.slot}"
        path_part = f" › {finding.path}" if finding.path else ""
        line_part = f" (line {finding.line})" if finding.line is not None else ""
        text = f"{prefix}{path_part}{line_part}: {finding.message}"
        if finding.severity == "warning":
            logger.warning(text)
        else:
            logger.error(text)

    logger.info(f"Dispenser profiles: {report.render_text().splitlines()[-1]}")

    return profiles


def wire_dispenser_profiles(vmc: VMC, profiles: DispenserProfiles) -> None:
    """Hand the loaded dispenser profiles to the VMC (CFG-101/CFG-102
    reconciliation) and to the routes module. Extracted out of `main()`
    so this step can be exercised in a test without an event loop --
    `main()` itself is an infinite event loop under `@logger.catch()`,
    so it cannot be run partially; this is the same two calls `main()`
    makes, just moved into a function, and changes none of `main()`'s
    own behaviour.

    `web_interface.routes` is imported at module scope here: it does not
    import anything that reaches back into `services.startup_dispensers`
    (verified empirically -- importing this module standalone succeeds),
    so there is no cycle to work around.
    """
    vmc.set_dispenser_profiles(profiles)
    routes.set_dispenser_profiles(profiles)
