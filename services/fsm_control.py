# fsm_control.py
from loguru import logger

def perform_command(command: str) -> str:
    logger.info(f"[Admin] Received command: {command}")

    match command:
        case "restart":
            logger.info("🔄 Restarting machine...")
            # TODO: Actual restart logic
            return "Restart command sent"

        case "reset":
            logger.info("♻️ Resetting system...")
            # TODO: Actual reset logic
            return "Reset command sent"

        case "shutdown":
            logger.info("⏹️ Shutting down machine...")
            # TODO: Actual shutdown logic
            return "Shutdown command sent"

        case _:
            logger.warning(f"❓ Unknown command: {command}")
            return f"Unknown command: {command}"
