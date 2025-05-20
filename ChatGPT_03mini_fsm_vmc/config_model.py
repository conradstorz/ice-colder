""" new proposed layout for config_model.py
# This file defines the configuration model for the vending machine controller.
# It uses Pydantic for data validation and provides a structured way to manage
# machine configuration, including physical details, payment methods, and       
# owner contacts.
# The model is designed to be extensible and maintainable, allowing for easy
# updates and additions to the configuration as needed.
# The model includes:
# - Person: A generic person record with name, email, and phone.
# - PeopleConfig: A bundle of roles for machine owner, location owner, and service technicians.
# - PhysicalDetails: Metadata for the physical machine, including serial number, location, and products.
# - StripeConfig: Configuration for Stripe payment gateway.
# - PayPalConfig: Configuration for PayPal payment gateway.
# - MDBDevice: Configuration for MDB devices on the bus.
# - MDBConfig: Configuration for the MDB bus, including polling interval and devices.
# - PaymentConfig: A single configuration that holds all payment methods.
# - ConfigModel: The top-level model that combines all configurations.
# The model also includes convenience properties for easy access to various
# configuration details, such as products, machine owner, and payment methods.
# The model uses Pydantic's BaseModel for data validation and provides
# type hints for better code readability and maintainability.
# The model is designed to be used in a vending machine controller application,
# where it can be loaded from a configuration file (e.g., YAML or JSON) and
# used to initialize the machine's settings and behavior.
# The model is extensible, allowing for easy addition of new fields or
# configurations as needed in the future.
# The model also includes logging functionality to track when models are
# successfully created or loaded, providing better visibility into the
# configuration process and helping with debugging and maintenance.


from typing import List, Optional, Dict
from pydantic import BaseModel, EmailStr, SecretStr, Field

# 1) A generic person record
class Person(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    # you could add an enum role, but we'll group by field below

# 2) Bundle all of your “people” roles in one place
class PeopleConfig(BaseModel):
    machine_owner: Person
    location_owner: Person
    service_technicians: List[Person] = Field(
        default_factory=list,
        description="One or more techs who can service the machine"
    )
    # if you later need “backup_owner” or “QA_inspector” you can add them here

# 3) Your physical‐machine metadata
class PhysicalDetails(BaseModel):
    serial_number: str
    location: str  # e.g. “Lobby, Building A”
    model: Optional[str]
    people: PeopleConfig
    products: List[Dict]  # e.g. [{"sku": "...", "price": 1.25}, ...]

# 4) Gateway‐specific configs
class StripeConfig(BaseModel):
    api_key: SecretStr
    webhook_secret: SecretStr
    max_retry_delay: int = Field(
        10,
        description="Seconds to wait before retrying a failed Stripe call"
    )

class PayPalConfig(BaseModel):
    client_id: str
    client_secret: SecretStr
    sandbox: bool = True

# 5) MDB devices on the bus
class MDBDevice(BaseModel):
    address: int
    device_type: str    # e.g. “bill_validator”, “coin_acceptor”
    settings: Dict[str, str] = {}

class MDBConfig(BaseModel):
    polling_interval: int = 5  # seconds between bus polls
    devices: List[MDBDevice]

# 6) A single PaymentConfig that holds all of them
class PaymentConfig(BaseModel):
    stripe: Optional[StripeConfig] = None
    paypal: Optional[PayPalConfig] = None
    mdb: MDBConfig

# 7) Your top‐level model
class ConfigModel(BaseModel):
    physical: PhysicalDetails
    payment: PaymentConfig

    # --- convenience properties ---
    @property
    def products(self) -> List[Dict]:
        return self.physical.products

    @property
    def machine_owner(self) -> Person:
        return self.physical.people.machine_owner

    @property
    def location_owner(self) -> Person:
        return self.physical.people.location_owner

    @property
    def service_technicians(self) -> List[Person]:
        return self.physical.people.service_technicians

    @property
    def stripe(self) -> Optional[StripeConfig]:
        return self.payment.stripe

    @property
    def paypal(self) -> Optional[PayPalConfig]:
        return self.payment.paypal

    @property
    def mdb_devices(self) -> List[MDBDevice]:
        return self.payment.mdb.devices


How this helps
Separation of concerns

Everyone (Person) is defined once, then wired into PeopleConfig.

Gateways each get their own clear namespace (stripe, paypal, mdb).

One place to change
If you add a new role (backup_owner) or a new gateway (square), you only touch the models and the handful of flat properties you care about.

Type safety & IDE autocompletion
Anywhere in your code that you previously wrote

cfg["physical_details"]["people"]["machine_owner"]["email"]
you can now do

cfg.machine_owner.email
with full type checks and docs.

Extensible

Add extra fields (e.g. phone, backup_contact) to Person—every role gets them automatically.

Tweak MDBConfig.polling_interval or StripeConfig.max_retry_delay only in one spot.

With this layout, your VMC code simply holds on to a ConfigModel and calls the high-level properties you need—no more .model_dump() or nested dict fiddling.
"""


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
