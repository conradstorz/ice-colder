from pydantic import BaseModel, Field, model_validator
from typing import List, Dict, Any
from loguru import logger

class OwnerContact(BaseModel):
    name: str
    phone_number: str
    address: str
    email: str
    notes: str

    @model_validator(mode="after")
    def log_owner_contact(cls, values):
        # Log when an OwnerContact model is successfully created
        logger.debug(f"{cls.__name__} loaded: name={values.name}, email={values.email}")
        return values

class PhysicalDetails(BaseModel):
    machine_id: str
    products: List[Dict[str, Any]]

    @model_validator(mode="after")
    def log_physical_details(cls, values):
        # Log when PhysicalDetails model is successfully created
        product_count = len(values.products) if values.products is not None else 0
        logger.debug(f"{cls.__name__} loaded: machine_id={values.machine_id}, products count={product_count}")
        return values

class ConfigModel(BaseModel):
    machine_owner_contact: OwnerContact
    location_owner_contact: OwnerContact
    physical_location: OwnerContact
    physical_details: PhysicalDetails

    @model_validator(mode="after")
    def log_config_summary(cls, values):
        # Log a summary when the full configuration model is loaded
        pd = values.physical_details
        product_count = len(pd.products) if pd.products is not None else 0
        logger.info(f"{cls.__name__} loaded: machine_id={pd.machine_id}, products_count={product_count}")
        return values

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
