# main.py
import asyncio
from loguru import logger
from controller.vmc import VMC

# Configure loguru: Rotate log file at midnight
logger.add("vmc.log", rotation="00:00")

def main():
    logger.info("Starting Vending Machine Controller")
    vmc = VMC(config_file='config.json')
    try:
        asyncio.run(vmc.run())
    except KeyboardInterrupt:
        logger.info("Shutting down VMC due to keyboard interrupt.")

if __name__ == '__main__':
    main()
