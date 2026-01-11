# ========== vmc_physical.py ==========
"""
vmc_physical.py

EXAMPLE/TEMPLATE FILE - NOT CURRENTLY IN USE

Program that integrates the pure FSM (from vmc_core.py) with actual hardware interfaces,
user interface callbacks, and any external services (e.g., payment gateways, MDB bus).
"""
# Note: This file contains example code and is not currently imported or used.
# It serves as a template for physical hardware integration.

from vmc_core import VMC
from loguru import logger
from services.payment_gateway_manager import PaymentGatewayManager
from hardware.mdb_interface import MDBInterface

# Example setup: instantiate core FSM with real product/config data
# Load Pydantic ConfigModel elsewhere and extract necessary fields
# The following assumes `config_model` is already created and validated
# (this is example code - config_model would need to be passed in or loaded)

def setup_physical_vmc(config_model):
    """
    Example function showing how to set up the VMC with physical hardware.
    
    Args:
        config_model: A validated ConfigModel instance
    """

def setup_physical_vmc(config_model):
    """
    Example function showing how to set up the VMC with physical hardware.
    
    Args:
        config_model: A validated ConfigModel instance
    """
    # Initialize core FSM
    core_vmc = VMC(
        products=config_model.physical.products,
        owner_contact=config_model.machine_owner,
    )

    # Attach physical services
    # Payment gateway manager handles virtual payment URL creation and QR code generation
    core_vmc.payment_gateway_manager = PaymentGatewayManager(
        config=config_model.payment.model_dump()
    )

    # MDB hardware interface for coin/payment bus communication
    core_vmc.mdb_interface = MDBInterface()

    # Define UI callbacks to connect with real display or GUI toolkit
    @logger.catch()
    def update_ui(state, selected, escrow):
        # Refresh graphical display: highlight state, show selected product and balance
        pass  # Replace with actual UI update logic

    @logger.catch()
    def display_message(msg):
        # Show text message on screen or terminal
        pass  # Replace with actual message display

    @logger.catch()
    def display_qr(image):
        # Render QR code image for user scanning
        pass  # Replace with actual image display

    # Register callbacks
    core_vmc.set_update_callback(update_ui)
    core_vmc.set_message_callback(display_message)
    core_vmc.set_qrcode_callback(display_qr)

    # Start MDB monitoring loop (asynchronous)
    import asyncio
    asyncio.create_task(core_vmc.start_mdb_monitoring())
    
    return core_vmc

# The rest of the application would handle GUI event loop, user inputs,
# and periodically call core_vmc.deposit_funds, select_product, etc.,
# responding to callbacks for UI and hardware events.
