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

if __name__ == "__main__":
    import json

    with open("config.json", "r") as f:
        config_data = json.load(f)

    try:
        config = ConfigModel(**config_data)
        print("Configuration loaded successfully:")
        # Use json.dumps() to pretty-print the output from model_dump()
        print(json.dumps(config.model_dump(), indent=2))
    except Exception as e:
        print("Failed to load configuration:")
        print(e)
