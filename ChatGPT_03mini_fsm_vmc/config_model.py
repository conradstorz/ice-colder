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
from pydantic.networks import PhoneStr
from loguru import logger


# 1) First, an enum of supported communication channels
class Channel(str, Enum):
    email    = "email"
    sms      = "sms"
    snapchat = "snapchat"
    # …add more as you integrate them

# 2) A generic person record including preferred comms
class Person(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[PhoneStr] = None
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

# 3) Gateway-specific communications configs
class EmailGatewayConfig(BaseModel):
    provider: str = "smtp"
    # e.g. "smtp.example.com" or "smtp.gmail.com"
    # or "smtp.sendgrid.net" or "smtp.mailgun.org"
    smtp_server: str
    port: int = 587
    username: SecretStr
    password: SecretStr
    default_from: EmailStr

class SMSGatewayConfig(BaseModel):
    # e.g. Twilio, Nexmo, etc.
    # This is a simplified example; real-world usage may require more fields
    # like API keys, sender numbers, etc.
    provider: str = "twilio"    
    account_sid: SecretStr
    auth_token: SecretStr
    from_number: PhoneStr

class SnapchatGatewayConfig(BaseModel):
    client_id: SecretStr
    client_secret: SecretStr
    # …any other Snapchat API settings…

# 4) Bundle them under one CommunicationConfig
class CommunicationConfig(BaseModel):
    email: Optional[EmailGatewayConfig] = None
    sms: Optional[SMSGatewayConfig] = None
    snapchat: Optional[SnapchatGatewayConfig] = None
    # …add more as you integrate them
    # e.g. push notifications, webhooks, etc.

    def get_gateway(self, channel: Channel) -> Optional[BaseModel]:
        """
        Return the config for the given channel, or None if not configured.
        """
        return getattr(self, channel.value, None)

# 5) Bundle all of your “people” roles in one place
class PeopleConfig(BaseModel):
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

# 6) Define a first-class Product model
class Product(BaseModel):
    sku: str
    price: float
    name: Optional[str]  = None
    description: Optional[str] = None
    image_url: Optional[str] = None # e.g. “https://example.com/image.jpg”  
    inventory_count: int = default=0 # e.g. 0 for out of stock, 10 for 10 items in stock, -1 for unlimited
    # …any other product-specific fields you want to add

# Your physical machine model     
class PhysicalDetails(BaseModel):
    common_name: str  # e.g. “Vending Machine 1” or “Snack Machine” or “Coffee Machine”
    serial_number: str
    location: str  # e.g. “Lobby, Building A” or “Floor 2, Room 201” or “Warehouse 3”
    model: Optional[str]
    people: PeopleConfig  # e.g. {"machine_owner": Person, …}
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
        # Log when a PhysicalDetails model is successfully created
        # This is a good place to check if the serial number is unique
        count = len(values.product_list) if values.product_list else 0
        logger.debug(f"{cls.__name__} loaded: serial_number={values.serial_number}, location={values.location}, products count={count}")
        return values

# 8) Payment Gateway‐specific configs
class StripeConfig(BaseModel):
    api_key: SecretStr
    webhook_secret: SecretStr
    max_retry_delay: int = Field(
        10,
        description="Seconds to wait before retrying a failed Stripe call"
    )

class PayPalConfig(BaseModel):
    client_id: SecretStr
    client_secret: SecretStr
    sandbox: bool = True

# add more as needed, e.g. Square, Venmo, etc.

# 8) Define MDB devices on the bus
class MDBDevice(BaseModel):
    exists: bool = False
    serial_number: Optional[str] = None
    buss_address: int
    device_type: str    # e.g. "bill_validator", "coin_acceptor"
    settings: Dict[str, str] = {}

class MDBDevicesConfig(BaseModel):
    polling_interval: float = 0.5  # seconds between bus polls
    devices: List[MDBDevice]

# 10) A single PaymentConfig that holds all of them
class PaymentConfig(BaseModel):
    stripe: Optional[StripeConfig] = None
    paypal: Optional[PayPalConfig] = None
    mdb: Optional[MDBDevicesConfig] = None

# 11) Define a uniform adapter interface
class GatewayAdapter(Protocol):
    def send(self, to: str, body: str, **kwargs) -> None: ...
    def start_receiving(self, callback: Callable[[str, str], None]) -> None: ...

# 12) Top-level model now includes communication
class ConfigModel(BaseModel):
    physical: PhysicalDetails
    payment: PaymentConfig
    communication: CommunicationConfig

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
        """Access all gateway configs in one place."""
        return self.communication

    def get_preferred_gateway_for(self, person: Person):
        """
        Walk the person’s `preferred_comm` list in order,
        returning the first configured gateway and its channel.
        """
        for channel in person.preferred_comm:
            gw = self.comm.get_gateway(channel)
            if gw is not None:
                return channel, gw
        return None, None


