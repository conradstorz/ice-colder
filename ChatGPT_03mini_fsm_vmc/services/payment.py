# services/payment.py
import random
from loguru import logger

class PaymentService:
    def process_payment(self):
        logger.info("Processing payment via Payment Service.")
        success = random.choice([True, False])
        if success:
            logger.debug("Payment processed successfully.")
        else:
            logger.error("Payment processing failed.")
        return success
