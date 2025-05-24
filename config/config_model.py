# This file is part of the Vending Machine Controller project.
"""
 new proposed layout for config_model.py

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

Add extra fields (e.g. phone, backup_contact) to Person, then every role gets them automatically.

Tweak MDBConfig.polling_interval or StripeConfig.max_retry_delay only in one spot.

With this layout, your VMC code simply holds on to a ConfigModel and calls the high-level properties you need—no more .model_dump() or nested dict fiddling.

Define a uniform gateway interface (e.g. send(to, body, **kwargs) and start_receiving(callback))

Implement one adapter class per channel using the patterns above

Wire them into your CommunicationConfig so cfg.comm.email returns your SMTP/SES client, cfg.comm.slack your Slack client, etc.

With that in place, adding support for any new channel is as simple as:

Adding its config model

Writing a tiny adapter class

Exposing it in your façade

Your VMC logic then just does:

channel_name, gateway_handle = cfg.get_preferred_gateway_for(person)
gateway_handle.send(person_contact, message_body)
—no messy protocol details scattered throughout your code.


"""

# config_model.py

from enum import Enum
from typing import List, Optional, Dict, Any, Protocol, Callable
from pydantic import BaseModel, EmailStr, SecretStr, Field, model_validator
from pydantic_settings import BaseSettings
from pydantic_extra_types.phone_numbers import PhoneNumber
from loguru import logger
from datetime import datetime


# 1) First, an enum of supported communication channels
class Channel(str, Enum):
    """
    Supported communication channels
    """
    email = "email"
    sms = "sms"
    snapchat = "snapchat"
    # …add more channels as needed


class Person(BaseModel):
    """
    Generic person record with contact details and preferred communication channels
    """
    name: str
    email: EmailStr
    phone: Optional[PhoneNumber] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    preferred_comm: List[Channel] = Field(
        default_factory=list,
        description="Ordered list of channels to try when contacting this person"
    )

    @model_validator(mode="after")
    def log_owner_contact(cls, values):
        # Log when a Person model is successfully created
        logger.debug(f"{cls.__name__} loaded: name={values.name}, email={values.email}")
        # You can also log other fields if needed
        if values.phone:
            logger.debug(f"Phone: {values.phone}")
        return values

# 2) Gateway-specific communications configs
class EmailGatewayConfig(BaseModel):
    """
    Configuration for SMTP-based email gateway
    """
    provider: str = "smtp"
    smtp_server: str
    port: int = 587
    username: SecretStr
    password: SecretStr
    default_from: EmailStr


class SMSGatewayConfig(BaseModel):
    """
    Configuration for SMS gateway (e.g., Twilio)
    """
    provider: str = "twilio"
    account_sid: SecretStr
    auth_token: SecretStr
    from_number: PhoneNumber


class SnapchatGatewayConfig(BaseModel):
    """
    Configuration for Snapchat messaging gateway
    """
    client_id: SecretStr
    client_secret: SecretStr
    # …additional Snapchat API settings…


# 3) Bundle them under one CommunicationConfig
class CommunicationConfig(BaseModel):
    """
    Bundle of configured communication gateways
    """
    email: Optional[EmailGatewayConfig] = None
    sms: Optional[SMSGatewayConfig] = None
    snapchat: Optional[SnapchatGatewayConfig] = None
    # …add more channels here as integrated

    @logger.catch()
    def get_gateway(self, channel: Channel) -> Optional[BaseModel]:
        """
        Return the config model for a given channel, or None if not configured
        """
        return getattr(self, channel.value, None)

# 4) Bundle all of your “people” roles in one place
class PeopleConfig(BaseModel):
    """
    Roles and their associated Person records
    """
    machine_owner: Person
    location_owner: Person
    service_technicians: List[Person] = Field(
        default_factory=list,
        description="One or more techs who can service the machine"
    )
    # if you later need “backup_owner” or “QA_inspector” you can add them here
    # and they’ll automatically be available in the top-level model
    # e.g. cfg.physical.people.backup_owner
    # or cfg.physical.people.service_technicians[0].preferred_comm
    # …any other roles you want to add

# 5) Define a first-class Product model
class Product(BaseModel):
    """
    Product definition for vending
    """
    sku: str
    price: float
    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    inventory_count: int = Field(
        default=0,
        description="0=out of stock, -1=unlimited"
    )

# Define the physical machine model
class PhysicalDetails(BaseModel):
    """
    Metadata for the physical vending machine
    """
    common_name: str  # e.g. “Vending Machine 1” or “Snack Machine” or “Coffee Machine”
    serial_number: str
    location: str  # e.g. “Lobby, Building A” or “Floor 2, Room 201” or “Warehouse 3”
    model: Optional[str]
    people: PeopleConfig
    product_list: List[Product] = Field(..., alias="products")

    # --- convenience properties ---
    @property
    def machine_id(self) -> str:
        return self.serial_number

    @property
    def machine_location(self) -> str:
        return self.location

    @property
    def machine_model(self) -> Optional[str]:
        return self.model

    @property
    def machine_owner(self) -> Person:
        return self.people.machine_owner

    @property
    def location_owner(self) -> Person:
        return self.people.location_owner

    @property
    def service_technicians(self) -> List[Person]:
        return self.people.service_technicians

    @property
    def products(self) -> List[Product]:
        return self.product_list

    @model_validator(mode="after")
    def log_physical_details(cls, values):
        """
        Log when PhysicalDetails is loaded
        """
        count = len(values.product_list) if values.product_list else 0
        logger.debug(
            f"{cls.__name__} loaded: serial={values.serial_number}, "
            f"location={values.location}, products={count}"
        )
        return values

# 6) Payment Gateway‐specific configs
class StripeConfig(BaseModel):
    """
    Configuration for Stripe payment gateway
    """
    api_key: SecretStr
    webhook_secret: SecretStr
    max_retry_delay: int = Field(
        10,
        description="Seconds to wait before retrying a failed Stripe call"
    )


class PayPalConfig(BaseModel):
    """
    Configuration for PayPal payment gateway
    """
    client_id: SecretStr
    client_secret: SecretStr
    sandbox: bool = True


class MDBDevice(BaseModel):
    """
    Definition of a single MDB bus device
    """
    exists: bool = False
    serial_number: Optional[str] = None
    buss_address: int
    device_type: str  # e.g. "bill_validator", "coin_acceptor"
    settings: Dict[str, Any] = Field(default_factory=dict) # Device-specific settings 


class MDBDevicesConfig(BaseModel):
    """
    MDB bus polling and device list
    """
    polling_interval: float = Field(
        0.5,
        description="Seconds between bus polls"
    )
    devices: List[MDBDevice]

# 7) A single PaymentConfig that holds all of them
class PaymentConfig(BaseModel):
    """
    Combined payment gateway configurations
    """
    stripe: Optional[StripeConfig] = None
    paypal: Optional[PayPalConfig] = None
    mdb: Optional[MDBDevicesConfig] = None

# 8) Define a uniform adapter interface
class GatewayAdapter(Protocol):
    """
    Uniform gateway interface
    """
    @logger.catch()
    def send(self, to: str, body: str, **kwargs) -> None: ...

    @logger.catch()
    def start_receiving(self, callback: Callable[[str, str], None]) -> None: ...

# 9) Top-level model now includes communication
class ConfigModel(BaseModel):
    """
    Top-level configuration model for the VMC
    """
    physical: PhysicalDetails
    payment: PaymentConfig
    communication: CommunicationConfig
    # Allow loading from .env files via Pydantic BaseSettings
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # --- convenience properties ---
    @property
    def products(self) -> List[Product]:
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
    def mdb_devices(self) -> Optional[List[MDBDevice]]:
        return self.payment.mdb.devices if self.payment.mdb else None

    @property
    def comm(self) -> CommunicationConfig:
        """Access all communication gateway configs in one place."""
        return self.communication

    def get_preferred_gateway_for(
        self, person: Person
    ) -> Optional[tuple[Channel, BaseModel]]:
        """
        Return first configured gateway for a person in their preference order
        """
        for channel in person.preferred_comm:
            gateway = self.comm.get_gateway(channel)
            if gateway:
                return channel, gateway
        return None