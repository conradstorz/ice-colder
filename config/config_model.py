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

class ConfigModel(BaseModel): holds high-level fields for version, physical, payment, communication

"""

from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, EmailStr, Field, SecretStr, model_validator


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
    Generic person record with contact details and preferred channels
    """
    name: str = Field("Your Name", description="Full name of the person")
    email: EmailStr = Field("user@example.com", description="Email address")
    phone: Optional[str] = Field("123-456-7890", description="Phone number")
    address: Optional[str] = Field("123 Main St", description="Postal address")
    notes: Optional[str] = Field("Notes about person", description="Optional notes")
    preferred_comm: List[Channel] = Field(
        default_factory=lambda: [Channel.email],
        description="Preferred communication channels"
    )


class PeopleConfig(BaseModel):
    machine_owner: Person = Field(
        default_factory=Person,
        description="Primary machine owner contact"
    )
    location_owner: Person = Field(
        default_factory=Person,
        description="Primary location contact"
    )
    service_technicians: List[Person] = Field(
        default_factory=list,
        description="List of service technicians"
    )


class Location(BaseModel):
    address: str = Field("123 Main St", description="Physical address")
    notes: Optional[str] = Field("Location notes", description="Additional location info")


class Product(BaseModel):
    sku: str = Field("SAMPLE-SKU", description="Machine Selection Code / Product SKU")
    name: str = Field("Sample Product", description="Product name")
    description: Optional[str] = Field("A sample product", description="Product description")
    image_url: Optional[str] = Field(
        "https://example.com/image.jpg",
        description="URL to product image"
    )
    price: float = Field(1.00, description="Price in USD")
    track_inventory: bool = Field(False, description="Whether to track inventory")
    inventory_count: int = Field(0, description="Initial inventory count")


class PhysicalDetails(BaseModel):
    common_name: str = Field(
        "YOUR_MACHINE_NAME",
        description="Friendly machine name; edit before use"
    )
    serial_number: str = Field(
        "0000-0000",
        description="Hardware serial number"
    )
    location: Location = Field(
        default_factory=Location,
        description="Machine physical location"
    )
    people: PeopleConfig = Field(
        default_factory=PeopleConfig,
        description="Contact roles"
    )
    products: List[Product] = Field(
        default_factory=lambda: [Product()],
        description="List of products available"
    )

    # --- convenience properties ---
    @property
    def machine_id(self) -> str:
        return self.serial_number

    @property
    def machine_location(self) -> Location:
        return self.location

    @property
    def machine_owner(self) -> Person:
        return self.people.machine_owner

    @property
    def location_owner(self) -> Person:
        return self.people.location_owner

    @property
    def service_technicians(self) -> List[Person]:
        return self.people.service_technicians

    @model_validator(mode="after")
    def log_physical_details(cls, model):
        """
        Log when PhysicalDetails is loaded
        """
        count = len(model.products) if model.products else 0
        logger.debug(
            f"{cls.__name__} loaded: serial={model.serial_number}, "
            f"location={model.location}, products={count}"
        )
        return model


class StripeConfig(BaseModel):
    api_key: SecretStr = Field(
        default=SecretStr("sk_test_xxx"),
        description="Stripe API key (dummy value)"
    )
    webhook_secret: SecretStr = Field(
        default=SecretStr("whsec_xxx"),
        description="Stripe webhook secret"
    )


class PayPalConfig(BaseModel):
    client_id: SecretStr = Field(
        default=SecretStr("paypal_client_id"),
        description="PayPal client ID"
    )
    client_secret: SecretStr = Field(
        default=SecretStr("paypal_client_secret"),
        description="PayPal client secret"
    )
    sandbox: bool = Field(
        True,
        description="Use PayPal sandbox mode"
    )


class MDBDevice(BaseModel):
    name: str = Field(
        "Card Reader",
        description="MDB device name"
    )
    exists: bool = Field(
        False,
        description="Flag indicating device presence"
    )
    serial_number: Optional[str] = Field(
        "00000000",
        description="Device serial number"
    )
    settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Device-specific settings"
    )


class MDBDevicesConfig(BaseModel):
    """
    MDB bus polling and device list
    """
    polling_interval: float = Field(
        0.5,
        description="Polling interval in seconds"
    )
    devices: List[MDBDevice] = Field(
        default_factory=list,
        description="List of MDB devices on the bus"
    )


class PaymentConfig(BaseModel):
    stripe: StripeConfig = Field(
        default_factory=StripeConfig,
        description="Stripe payment gateway configuration"
    )
    paypal: Optional[PayPalConfig] = Field(
        default_factory=PayPalConfig,
        description="PayPal payment gateway configuration"
    )
    mdb: MDBDevicesConfig = Field(
        default_factory=MDBDevicesConfig,
        description="MDB bus configuration"
    )


class EmailGatewayConfig(BaseModel):
    smtp_server: str = Field(
        "smtp.example.com",
        description="SMTP server address"
    )
    smtp_port: int = Field(
        587,
        description="SMTP port"
    )
    username: str = Field(
        "user@example.com",
        description="SMTP username"
    )
    password: SecretStr = Field(
        default=SecretStr("password"),
        description="SMTP password"
    )
    default_from: str = Field(
        "user@example.com",
        description="Default From address"
    )


class SMSGatewayConfig(BaseModel):
    account_sid: SecretStr = Field(
        default=SecretStr("ACxxxxxxxxxxxxxxxxxxx"),
        description="Twilio account SID"
    )
    auth_token: SecretStr = Field(
        default=SecretStr("your_auth_token"),
        description="Twilio auth token"
    )
    from_number: str = Field(
        "+1234567890",
        description="Default SMS From number"
    )


class CommunicationConfig(BaseModel):
    email_gateway: EmailGatewayConfig = Field(
        default_factory=EmailGatewayConfig,
        description="Email gateway configuration"
    )
    sms_gateway: SMSGatewayConfig = Field(
        default_factory=SMSGatewayConfig,
        description="SMS gateway configuration"
    )
    snapchat_gateway: Optional[Dict[str, Any]] = Field(
        None,
        description="Snapchat gateway (optional)"
    )


class ConfigModel(BaseModel):
    """
    Top-level configuration for the Vending Machine Controller
    """
    version: str = Field(
        "1.0.0",
        description="Configuration schema version"
    )
    physical: PhysicalDetails = Field(
        default_factory=PhysicalDetails,
        description="Physical machine details"
    )
    payment: PaymentConfig = Field(
        default_factory=PaymentConfig,
        description="Payment gateway configurations"
    )
    communication: CommunicationConfig = Field(
        default_factory=CommunicationConfig,
        description="Communication channels configuration"
    )

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
    def stripe(self) -> StripeConfig:
        return self.payment.stripe

    @property
    def paypal(self) -> Optional[PayPalConfig]:
        return self.payment.paypal

    @property
    def mdb_devices(self) -> List[MDBDevice]:
        return self.payment.mdb.devices

    @property
    def comm(self) -> CommunicationConfig:
        """Access all communication gateway configs in one place."""
        return self.communication

    def get_preferred_gateway_for(
        self, person: Person
    ) -> Optional[tuple[Channel, Any]]:
        """
        Return first configured gateway for a person in their preference order.
        """
        for channel in person.preferred_comm:
            if channel == Channel.email and self.communication.email_gateway:
                return channel, self.communication.email_gateway
            if channel == Channel.sms and self.communication.sms_gateway:
                return channel, self.communication.sms_gateway
            if channel == Channel.snapchat and self.communication.snapchat_gateway:
                return channel, self.communication.snapchat_gateway
        return None
