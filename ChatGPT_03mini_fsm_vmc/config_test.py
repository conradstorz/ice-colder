from typing import List, Optional
from pydantic import BaseModel, model_validator

class Product(BaseModel):
    name: str
    price: float
    track_inventory: bool
    inventory_count: Optional[int] = None

    @model_validator(mode="after")
    def validate_inventory_count(cls, product: "Product") -> "Product":
        if product.track_inventory and product.inventory_count is None:
            raise ValueError("inventory_count must be provided when track_inventory is True")
        return product

class LocationOwnerContact(BaseModel):
    name: str
    phone_number: str
    address: str
    email: str
    sms: str

class PhysicalDetails(BaseModel):
    machine_id: str
    serial_number: str
    manufacturer: str
    model: str

class RepairServiceContactInfo(BaseModel):
    phone_number: str
    email_address: str

class RepairServiceDetails(BaseModel):
    name: str
    contact_info: RepairServiceContactInfo

class ConfigModel(BaseModel):
    products: List[Product]
    location_owner_contact: LocationOwnerContact
    physical_details: PhysicalDetails
    repair_service_details: RepairServiceDetails

"""Example config file access and update:
"""

def load_config(filepath: str = "config.json") -> ConfigModel:
    with open(filepath, "r") as f:
        config_data = json.load(f)
    return ConfigModel(**config_data)

def save_config(config: ConfigModel, filepath: str = "config.json"):
    with open(filepath, "w") as f:
        json.dump(config.model_dump(), f, indent=2)


if __name__ == "__main__":
    import json

    config = load_config("config.json")

    # Use json.dumps() to pretty-print the output from model_dump()
    print(json.dumps(config.model_dump(), indent=2))

    # Example of updating a product's price
    config.products[0].price = 2.50
    # Save the updated config back to the file
    save_config(config, "config.json")
    # Print the updated config to verify the change
    print(json.dumps(config.model_dump(), indent=2))

    # reload the config to verify the change
    updated_config = load_config("config.json")

    if config == updated_config:
        print("The updated and re-loaded configs are the same.")
    else:
        print("The original and updated configs are different.")

