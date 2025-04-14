from typing import List, Optional
from pydantic import BaseModel, model_validator
import json
import os
import random

# Define the Product model with defaults.
class Product(BaseModel):
    name: str = "Unnamed Product"
    price: float = 0.0
    track_inventory: bool = False
    # Default inventory_count provided; if tracking is on and inventory_count is missing,
    # the validator will raise an error.
    inventory_count: Optional[int] = 0

    @model_validator(mode="after")
    def validate_inventory_count(cls, product: "Product") -> "Product":
        if product.track_inventory and product.inventory_count is None:
            raise ValueError("inventory_count must be provided when track_inventory is True")
        return product

# Define the LocationOwnerContact model with sensible defaults.
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

# Define PhysicalDetails with a new field, for example, firmware_version.
class PhysicalDetails(BaseModel):
    machine_id: str = "VMC-123456"
    serial_number: str = "SN-123456789"
    manufacturer: str = "Royal Vendors Inc."
    model: str = "RV-5000"
    firmware_version: str = "1.0.0"  # Example default firmware version

# Define the contact info for the repair service.
class RepairServiceContactInfo(BaseModel):
    phone_number: str = "+1-555-123-8978"
    email_address: str = "support@ifixu.com"

# Define the repair service details with defaults.
class RepairServiceDetails(BaseModel):
    name: str = "Foo Bar Baz LLC"
    contact_info: RepairServiceContactInfo = RepairServiceContactInfo()

# Define the top-level ConfigModel with versioning and default values.
class ConfigModel(BaseModel):
    # Version in the format yyyy.mm.dd.xx (update as needed)
    config_version: str = "2025.04.14.01"
    products: List[Product] = [
        Product(name="Soda", price=1.25, track_inventory=True, inventory_count=10),
        Product(name="Chips", price=1.00, track_inventory=True, inventory_count=5),
        Product(name="Candy", price=0.75, track_inventory=False, inventory_count=0),
        Product(name="Water", price=1.00, track_inventory=False, inventory_count=0)
    ]
    location_owner_contact: LocationOwnerContact = LocationOwnerContact()
    physical_details: PhysicalDetails = PhysicalDetails()
    repair_service_details: RepairServiceDetails = RepairServiceDetails()

def migrate_config(config: ConfigModel, filepath: str = "config.json") -> ConfigModel:
    """
    Compare the loaded configuration version with the current default.
    If they differ, update the version and any other fields if necessary,
    then save the updated configuration back to the file.
    """
    default_config = ConfigModel()  # Create a default instance for comparison.
    if config.config_version != default_config.config_version:
        print(
            f"Migrating config from version {config.config_version} "
            f"to {default_config.config_version}"
        )
        # Update the version and (if needed) other fields.
        config.config_version = default_config.config_version
        # Additional migration logic can go here.
        save_config(config, filepath)
    return config

def load_config(filepath: str = "config.json") -> ConfigModel:
    # If the configuration file doesn't exist, create one with default values.
    if not os.path.exists(filepath):
        config = ConfigModel()
        save_config(config, filepath)
        return config

    # Otherwise, load the config file.
    with open(filepath, "r") as f:
        config_data = json.load(f)
    config = ConfigModel(**config_data)
    # Migrate the config if its version is outdated.
    config = migrate_config(config, filepath)
    return config

def save_config(config: ConfigModel, filepath: str = "config.json"):
    with open(filepath, "w") as f:
        json.dump(config.model_dump(), f, indent=2)

if __name__ == "__main__":
    # Load the configuration from file (or create defaults if missing)
    config = load_config("config.json")
    print("Loaded configuration:")
    print(json.dumps(config.model_dump(), indent=2))

    # Example: randomly update the price of the first product.
    config.products[0].price = round(random.uniform(0.01, 100.00), 2)
    
    # Save the updated configuration back to disk.
    save_config(config, "config.json")
    print("Updated configuration:")
    print(json.dumps(config.model_dump(), indent=2))

    # Reload the configuration to verify that changes persisted.
    updated_config = load_config("config.json")
    if config == updated_config:
        print("The updated and re-loaded configs are the same.")
    else:
        print("The original and updated configs are different.")
