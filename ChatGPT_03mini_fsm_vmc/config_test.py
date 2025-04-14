from typing import List, Optional
from pydantic import BaseModel, model_validator, ValidationError
import json
import os
import random
from loguru import logger

# Setup Loguru: log debug messages to a file with rotation and also print to console.
logger.add("config_debug.log", level="DEBUG", rotation="1 MB")

# Define the Product model with defaults.
class Product(BaseModel):
    name: str = "Unnamed Product"
    price: float = 0.0
    track_inventory: bool = False
    inventory_count: Optional[int] = 0

    @model_validator(mode="after")
    def validate_inventory_count(cls, product: "Product") -> "Product":
        if product.track_inventory and product.inventory_count is None:
            raise ValueError("inventory_count must be provided when track_inventory is True")
        logger.debug(f"Validated Product: {product.name}")
        return product

# Define the Contact model used by both location and machine owner contacts.
class Contact(BaseModel):
    name: str = "John Doe"
    phone_number: str = "+1-555-123-4567"
    address: str = "123 Main St, City, State, ZIP"
    email: str = "owner@example.com"
    sms: str = "+15555555555"

class LocationOwnerContact(BaseModel):
    contact_info: Contact = Contact()

class MachineOwnerContact(BaseModel):
    contact_info: Contact = Contact()

# Define PhysicalDetails with a firmware_version as an example of an extra field.
class PhysicalDetails(BaseModel):
    machine_id: str = "VMC-123456"
    serial_number: str = "SN-123456789"
    manufacturer: str = "Royal Vendors Inc."
    model: str = "RV-5000"
    firmware_version: str = "1.0.0"  # Default firmware version

# Define the repair service contact info.
class RepairServiceContactInfo(BaseModel):
    phone_number: str = "+1-555-123-8978"
    email_address: str = "support@ifixu.com"

class RepairServiceDetails(BaseModel):
    name: str = "Foo Bar Baz LLC"
    contact_info: RepairServiceContactInfo = RepairServiceContactInfo()

# Define the top-level ConfigModel with versioning and default values.
class ConfigModel(BaseModel):
    # Version in the format yyyy.mm.dd.xx (update this as needed)
    config_version: str = "2025.04.14.02"
    products: List[Product] = [
        Product(name="Soda", price=1.25, track_inventory=True, inventory_count=10),
        Product(name="Chips", price=1.00, track_inventory=True, inventory_count=5),
        Product(name="Ice", price=0.75, track_inventory=False, inventory_count=0),
        Product(name="Water", price=1.00, track_inventory=False, inventory_count=0)
    ]
    location_owner_contact: LocationOwnerContact = LocationOwnerContact()
    machine_owner_contact: MachineOwnerContact = MachineOwnerContact()
    physical_details: PhysicalDetails = PhysicalDetails()
    repair_service_details: RepairServiceDetails = RepairServiceDetails()

def save_config(config: ConfigModel, filepath: str = "config.json"):
    try:
        with open(filepath, "w") as f:
            json.dump(config.model_dump(), f, indent=2)
        logger.info(f"Configuration saved successfully to {filepath}.")
    except Exception as e:
        logger.exception(f"Failed to save configuration: {e}")

def migrate_config(config: ConfigModel, filepath: str = "config.json") -> ConfigModel:
    """
    Compare the loaded configuration version with the current default.
    If they differ, update the version and any other fields if necessary,
    then save the updated configuration back to the file.
    """
    default_config = ConfigModel()  # Create a default instance for comparison.
    if config.config_version != default_config.config_version:
        logger.info(
            f"Migrating config from version {config.config_version} "
            f"to {default_config.config_version}."
        )
        # Update the version and (if needed) other fields.
        config.config_version = default_config.config_version
        # Additional migration logic can be added here.
        save_config(config, filepath)
    else:
        logger.debug("No migration needed; version is up-to-date.")
    return config

def load_config(filepath: str = "config.json") -> ConfigModel:
    # If the configuration file doesn't exist, create one with default values.
    if not os.path.exists(filepath):
        logger.info(f"Configuration file {filepath} not found. Creating new config with defaults.")
        config = ConfigModel()
        save_config(config, filepath)
        return config

    try:
        with open(filepath, "r") as f:
            config_data = json.load(f)
        logger.debug("Configuration file loaded successfully.")
        config = ConfigModel(**config_data)
        # Migrate the config if its version is outdated.
        config = migrate_config(config, filepath)
        return config
    except json.JSONDecodeError as e:
        logger.exception(f"JSON decode error in configuration file: {e}")
        raise
    except ValidationError as e:
        logger.exception(f"Pydantic validation error: {e}")
        raise
    except Exception as e:
        logger.exception(f"Unexpected error while loading configuration: {e}")
        raise

if __name__ == "__main__":
    try:
        # Load the configuration from file (or create defaults if missing).
        config = load_config("config.json")
        logger.info("Loaded configuration:")
        logger.debug(json.dumps(config.model_dump(), indent=2))

        # Example: randomly update the price of the first product.
        old_price = config.products[0].price
        new_price = round(random.uniform(0.01, 100.00), 2)
        config.products[0].price = new_price
        logger.info(f"Updated product 'Soda' price from {old_price} to {new_price}.")

        # Save the updated configuration back to disk.
        save_config(config, "config.json")

        # Reload the configuration to verify that changes persisted.
        updated_config = load_config("config.json")
        if config == updated_config:
            logger.info("The updated and re-loaded configs are the same.")
        else:
            logger.error("The original and updated configs are different.")
    except Exception as e:
        logger.exception(f"Fatal error in main: {e}")
