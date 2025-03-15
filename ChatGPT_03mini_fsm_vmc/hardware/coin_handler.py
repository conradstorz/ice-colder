# hardware/coin_handler.py
import random
from loguru import logger

class CoinHandler:
    def insert_coin(self):
        logger.info("Coin inserted into coin handler.")
        success = random.choice([True, False])
        if success:
            logger.debug("Coin accepted by the coin handler.")
        else:
            logger.error("Coin rejected by the coin handler.")
        return success
