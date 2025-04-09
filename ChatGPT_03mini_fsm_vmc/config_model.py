from pydantic import BaseModel, Field
from typing import List, Dict, Any

class OwnerContact(BaseModel):
    name: str
    phone_number: str
    address: str
    email: str
    notes: str

class PhysicalDetails(BaseModel):
    machine_id: str
    serial_number: str
    manufacturer: str
    model: str
    year_of_manufacture: int
    products: List[Dict[str, Any]]
    dimensions: Dict[str, int]
    user_interactive_display: Dict[str, int]
    weight_in_pounds: int
    power_requirements: Dict[str, Any]

class ConfigModel(BaseModel):
    machine_owner_contact: OwnerContact
    location_owner_contact: OwnerContact
    physical_location: OwnerContact
    physical_details: PhysicalDetails

