# services/payment.py
import random
from loguru import logger

class PaymentService:
    def process_payment(self, amount):
        logger.info(f"Processing payment of ${amount:.2f} via Payment Service.")
        success = random.choice([True, False])
        if success:
            logger.debug(f"Payment of ${amount:.2f} processed successfully.")
        else:
            logger.error(f"Payment of ${amount:.2f} failed.")
        return success

