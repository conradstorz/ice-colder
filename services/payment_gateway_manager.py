# services/payment_gateway_manager.py
import asyncio
import qrcode
from loguru import logger


class BaseGateway:
    def __init__(self, config):
        self.config = config

    async def monitor_incoming(self):
        """
        Stub method to simulate monitoring for incoming payments.
        In a real implementation, this would query the gateway's API.
        """
        logger.info(f"{self.__class__.__name__}: Checking for incoming payments...")
        await asyncio.sleep(0.5)
        logger.debug(f"{self.__class__.__name__}: No new payments found.")

    def generate_payment_url(self, amount: float) -> str:
        """
        Create a dummy payment URL for the gateway.
        """
        return (
            f"https://{self.__class__.__name__.lower()}.example.com/pay?amount={amount}"
        )

    def generate_qr_code(self, payment_url: str):
        """
        Generate a QR code image for the given payment URL.
        """
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(payment_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        return img


class StripeGateway(BaseGateway):
    pass


class PayPalGateway(BaseGateway):
    pass


class SquareGateway(BaseGateway):
    pass


class PaymentGatewayManager:
    def __init__(self, config: dict = None):
        """
        Initialize the manager with configuration for each gateway.
        The config dict can contain settings for 'stripe', 'paypal', and 'square'.
        """
        self.config = config or {}
        self.gateways = {
            "stripe": StripeGateway(self.config.get("stripe")),
            "paypal": PayPalGateway(self.config.get("paypal")),
            "square": SquareGateway(self.config.get("square")),
        }

    async def monitor_accounts(self):
        """
        Asynchronously monitor each payment gateway for incoming payments.
        Currently, this is a stub that logs a message for each gateway.
        """
        while True:
            for name, gateway in self.gateways.items():
                await gateway.monitor_incoming()
            await asyncio.sleep(5)  # Poll every 5 seconds

    def generate_qr_code(self, gateway_name: str, amount: float):
        """
        Generate a QR code image for a payment request using the specified gateway.
        """
        if gateway_name not in self.gateways:
            logger.error(
                f"PaymentGatewayManager: Gateway '{gateway_name}' is not supported."
            )
            return None
        gateway = self.gateways[gateway_name]
        payment_url = gateway.generate_payment_url(amount)
        logger.info(f"PaymentGatewayManager: Generating QR code for URL: {payment_url}")
        img = gateway.generate_qr_code(payment_url)
        return img


# Example usage (this code would typically be called from your FSM or UI code):
if __name__ == "__main__":
    import io
    from PIL import Image

    async def main():
        manager = PaymentGatewayManager()
        # Start monitoring accounts in the background
        asyncio.create_task(manager.monitor_accounts())
        # Generate a QR code for a $2.50 payment via Stripe
        qr_img = manager.generate_qr_code("stripe", 2.50)
        # For demonstration, save the QR code image to a BytesIO stream and display it
        img_bytes = io.BytesIO()
        qr_img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        # Open the image (if running on a desktop environment)
        img = Image.open(img_bytes)
        img.show()

    asyncio.run(main())
