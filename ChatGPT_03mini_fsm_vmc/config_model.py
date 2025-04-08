from pydantic import BaseModel, Field
from typing import List, Dict, Any

class OwnerContact(BaseModel):
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
    year_of_manufacture: int
    location: Dict[str, str]
    products: List[Dict[str, Any]]
    dimensions: Dict[str, int]
    user_interactive_display: Dict[str, int]
    weight_in_pounds: int
    capacity: Dict[str, int]
    type: str
    features: List[str]
    power_requirements: Dict[str, Any]
    security_features: List[str]

class ConfigModel(BaseModel):
    machine_owner_contact: Dict[str, Any] = {}
    location_owner_contact: OwnerContact
    physical_details: PhysicalDetails
    operational_parameters: Dict[str, Any] = {}
    purchase_details: Dict[str, Any] = {}
    ownership_details: OwnerContact
    repair_service_details: Dict[str, Any] = {}
    maintenance_details: Dict[str, Any] = {}
    inventory_details: Dict[str, Any] = {}
    virtual_payment_config: Dict[str, Any] = {}
    display_resolution: Dict[str, int] = Field(default_factory=lambda: {"width": 1024, "height": 768})
