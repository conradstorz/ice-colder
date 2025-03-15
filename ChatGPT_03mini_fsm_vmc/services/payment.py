# services/payment.py
import random
from loguru import logger

class PaymentService:
    def process_payment(self, amount):
        logger.info("Processing payment of ${:.2f} via Payment Service.", amount)
        success = random.choice([True, False])
        if success:
            logger.debug("Payment of ${:.2f} processed successfully.", amount)
        else:
            logger.error("Payment of ${:.2f} failed.", amount)
        return success
