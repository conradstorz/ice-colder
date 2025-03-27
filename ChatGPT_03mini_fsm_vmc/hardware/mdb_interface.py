# hardware/mdb_interface.py
import asyncio
import serial
from loguru import logger

class MDBInterface:
    def __init__(self, port="/dev/ttyAMA0", baudrate=9600):
        self.port = port
        self.baudrate = baudrate
        try:
            self.serial_conn = serial.Serial(port, baudrate, timeout=1)
            logger.info(f"MDBInterface: Connected to MDB bus on {port} at {baudrate} baud.")
        except Exception as e:
            logger.error(f"MDBInterface: Failed to open serial port {port}: {e}")
            self.serial_conn = None

    async def read_messages(self, message_handler):
        """
        Continuously read messages from the MDB bus and pass them to the message_handler callback.
        The message_handler should be a function that accepts a single argument (the parsed message).
        """
        if not self.serial_conn:
            logger.error("MDBInterface: Serial connection not established.")
            return

        while True:
            try:
                if self.serial_conn.in_waiting > 0:
                    raw_data = self.serial_conn.read(self.serial_conn.in_waiting)
                    # Process the raw_data into a message; here we call a simple parser.
                    message = self.parse_message(raw_data)
                    logger.debug(f"MDBInterface: Received message: {message}")
                    # Call the message handler (typically a method on your FSM or VMC)
                    message_handler(message)
                # Use a short sleep to poll frequently for near real-time performance.
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"MDBInterface: Error reading from serial: {e}")
                await asyncio.sleep(1)

    def send_command(self, command_bytes):
        """
        Send a command to the MDB bus. 'command_bytes' should be a bytes object that conforms to the MDB protocol.
        """
        if not self.serial_conn:
            logger.error("MDBInterface: Serial connection not established.")
            return
        try:
            self.serial_conn.write(command_bytes)
            logger.info(f"MDBInterface: Sent command: {command_bytes}")
        except Exception as e:
            logger.error(f"MDBInterface: Failed to send command: {e}")

    def parse_message(self, raw_data):
        """
        Parse raw data from the MDB bus into a structured message.
        This is a stub function and should be implemented according to the MDB protocol specification.
        """
        # For demonstration purposes, we'll simply return the raw_data.
        # In a real-world application, you would decode the MDB message format here.
        return raw_data
