# fsm_control.py
"""Translates admin dashboard commands into VMC actions."""

from loguru import logger


def perform_command(command: str, vmc=None) -> str:
    logger.info(f"[Admin] Received command: {command}")

    match command:
        case "restart":
            logger.info("Restarting machine...")
            # TODO: Actual restart logic (process-level; handled by Docker/systemd)
            return "Restart command sent"

        case "reset":
            if vmc is None:
                logger.error("Reset requested but no VMC instance is available")
                return "Reset failed: VMC not available"
            if vmc.state != "error":
                logger.info(f"Reset ignored: VMC state is '{vmc.state}', not 'error'")
                return f"Reset ignored: machine is in '{vmc.state}', not 'error'"
            vmc.reset_state()
            logger.info("VMC reset from error to idle by admin command")
            return "Reset complete: machine returned to idle"

        case "shutdown":
            logger.info("Shutting down machine...")
            # TODO: Actual shutdown logic
            return "Shutdown command sent"

        case _:
            logger.warning(f"Unknown command: {command}")
            return f"Unknown command: {command}"
