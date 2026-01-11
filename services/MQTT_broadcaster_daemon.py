import time

import paho.mqtt.client as mqtt

# MQTT broker configuration
broker = "localhost"  # Change to your broker's IP address if different from where this code runs.
port = 1883
topic = "home/sensor/temperature"

# Create a client instance
client = mqtt.Client("rpi_publisher")

# Connect to the MQTT broker
client.connect(broker, port)

# Continuously publish sensor data (simulated here as a constant value)
try:
    while True:
        temperature = 22.5  # Replace this with your sensor reading if available
        client.publish(topic, temperature)
        print(f"Published {temperature} to topic '{topic}'")
        time.sleep(
            5
        )  # Wait 5 seconds between publishes  # NOTE: This is a blocking call and should be avoided in production code.
except KeyboardInterrupt:
    print("Exiting and disconnecting from broker.")
    client.disconnect()
