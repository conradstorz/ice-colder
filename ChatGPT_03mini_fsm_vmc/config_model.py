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
    products: List[Dict[str, Any]]

class ConfigModel(BaseModel):
    machine_owner_contact: OwnerContact
    location_owner_contact: OwnerContact
    physical_location: OwnerContact
    physical_details: PhysicalDetails

"""config file layout notes:
machine:
    Details:
        Name:
        Products:
        Physical Specs:
        Virtual Payment Providers:
        Maintenance Records:
        etc...
    Owner:
        Name:
        Contact:
        etc...
    Loation:
        Address:
        Description
        Contact:
        GPS:
        etc...
    Repair Service:
        Name:
        Contact:
        etc...
"""
