import os
import sys
import tkinter as tk
from loguru import logger
from config.config_model import ConfigModel
from hardware.tkinter_ui import VendingMachineUI
import json  # For loading configuration
from pydantic import ValidationError  # Handle Pydantic validation errors
import shutil
import time

# Create the LOGS subdirectory if it doesn't exist
os.makedirs("LOGS", exist_ok=True)

# Remove any default logging handlers
logger.remove()
# JSON log file with rotation and retention settings
logger.add(
    "LOGS/vmc_{time:YYYY-MM-DD_HH-mm-ss}.log.json",
    serialize=True,
    rotation="00:00",
    retention="3 days",
    compression="zip"
)
# Add console logging for INFO and ERROR messages (plain text, with custom format)
logger.add(
    sys.stdout,
    level="INFO",
    serialize=False,
    format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}\n"
)

@logger.catch()
def main():
    logger.info("Starting Vending Machine Controller")

    # Ensure config.json exists; if not, generate a skeleton for user
    if not os.path.exists("config.json"):
        logger.warning("Configuration file 'config.json' not found. Generating skeleton with default values.")
        # Generate base skeleton from Pydantic model defaults
        # model_construct() creates a ConfigModel instance with any default / default_factory values defined in the model
        default_config = ConfigModel.model_construct().model_dump()
        default_config = ConfigModel.model_construct().model_dump()
        # Insert obvious dummy values for required fields
        # For example machine identification
        default_config.setdefault("physical", {})
        phys = default_config["physical"]
        phys.setdefault("common_name", "YOUR_MACHINE_NAME")
        phys.setdefault("serial_number", "0000-0000")
        phys.setdefault("location", {"address": "123 Main St", "notes": "Your address"})
        # People config
        default_config.setdefault("communication", {}).setdefault("email_gateway", {}).update({
            "smtp_server": "smtp.example.com",
            "smtp_port": 587,
            "username": "user@example.com",
            "password": "password"
        })
        default_config.setdefault("communication", {}).setdefault("sms_gateway", {}).update({
            "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "auth_token": "your_auth_token",
            "from_number": "+1234567890"
        })
        default_config.setdefault("payment", {})
        default_config["payment"].setdefault("stripe", {}).update({
            "api_key": "sk_test_xxx",
            "webhook_secret": "whsec_xxx"
        })
        # Minimal products list
        phys.setdefault("products", [{
            "name": "Sample Product",
            "price": 1.00,
            "track_inventory": False
        }])

        # Write skeleton file
        with open("config.json", "w", encoding="utf-8") as fw:
            json.dump(default_config, fw, indent=4)
        logger.info("Skeleton 'config.json' created. Please update it with your actual settings and re-run the program.")
        sys.exit(0)

    # Load user config
    try:
        logger.debug("Reading raw JSON from 'config.json'")
        orig_data = json.load(open("config.json", encoding="utf-8"))
    except Exception as e:
        logger.exception("Error reading 'config.json': %s", e)
        sys.exit(1)

    # Build a default config dict from Pydantic model_construct
    default_dict = ConfigModel.model_construct().model_dump()
    merged_data = _deep_merge(default_dict, orig_data)

    # Backup and write defaults if any missing keys were added
    if _defaults_applied(orig_data, merged_data):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = f"config.json.bak_{timestamp}"
        shutil.copy("config.json", backup_path)
        logger.info(f"Backed up original config to {backup_path}")
        with open("config.json", "w", encoding="utf-8") as fw:
            json.dump(merged_data, fw, indent=4)
        logger.info("Inserted default values into 'config.json'")

    # Validate merged config
    try:
        logger.debug("Validating merged configuration via Pydantic model")
        config_model = ConfigModel.model_validate(merged_data)
        version = getattr(config_model, "version", None)
        logger.info(
            "Configuration loaded successfully%s",
            f": version={version}" if version else ""
        )
    except ValidationError as ve:
        logger.error("Configuration validation failed with the following errors:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get('loc', []))
            msg = err.get('msg', '')
            logger.error(f"  • {loc}: {msg}")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error validating configuration")
        sys.exit(1)

    # Initialize Tkinter UI
    try:
        logger.debug("Initializing Tkinter root window and UI")
        root = tk.Tk()
        root.title("Vending Machine Controller")
        logger.debug("Instantiating VendingMachineUI with configuration model")
        app = VendingMachineUI(root, config_model=config_model)
    except Exception:
        logger.exception("Failed to initialize Tkinter UI")
        sys.exit(1)

    # Enter main loop
    try:
        logger.info("Entering Tkinter main loop")
        root.mainloop()
    except Exception:
        logger.exception("Error during Tkinter main loop")
    finally:
        logger.info("Tkinter main loop has exited")


def _deep_merge(default: dict, source: dict) -> dict:
    """
    Recursively merge source dict on top of default dict.
    """
    merged = {}
    # Merge defaults and source
    for key, val in default.items():
        if key in source:
            if isinstance(val, dict) and isinstance(source[key], dict):
                merged[key] = _deep_merge(val, source[key])
            else:
                merged[key] = source[key]
        else:
            merged[key] = val
    # Include any extra keys from source
    for key, val in source.items():
        if key not in merged:
            merged[key] = val
    return merged


def _defaults_applied(orig: dict, merged: dict) -> bool:
    """
    Detect if merged contains keys not in orig (i.e., defaults applied).
    """
    for key in merged:
        if key not in orig:
            return True
        if isinstance(merged[key], dict) and isinstance(orig.get(key), dict):
            if _defaults_applied(orig[key], merged[key]):
                return True
    return False

if __name__ == "__main__":
    main()
