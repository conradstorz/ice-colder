import time
import random
import datetime
from config_handler import ConfigHandler

class IceVendingMachine:
    def __init__(self, config_file="config.txt"):
        self.config = ConfigHandler(config_file).get_config()

        self.machine_name = self.config.get("Machine", "Name")
        self.location = self.config.get("Machine", "Location")
        self.owner = self.config.get("Machine", "Owner")
        self.regular_ice_price = float(self.config.get("Pricing", "RegularIce"))
        self.premium_ice_price = float(self.config.get("Pricing", "PremiumIce"))
        self.water_price = float(self.config.get("Pricing", "Water"))
        self.ice_available = True
        self.payment_methods = self.config.get("Payment", "Methods").split(",")
        self.warning_email = self.config.get("Warnings", "Email")
        self.warning_sms = self.config.get("Warnings", "SMS")
        self.greeting = self.config.get("Greetings", "Greeting")

        self.hard_cash_credit = 0.0
        self.virtual_credit = 0.0
        self.hard_cash_total = 0.0
        self.virtual_total = 0.0
        self.machine_state = "idle"
        self.last_interaction_time = None
        self.timeout_duration = 60
        self.sales_data = []
        self.ice_dispensed = 0

    def display_menu(self):
        print(f"--- {self.machine_name} - {self.location} ---")
        print(f"{self.greeting}, {self.owner}!")
        print(f"1. Regular Ice (${self.regular_ice_price:.2f})")
        print(f"2. Premium Ice (${self.premium_ice_price:.2f})")
        print(f"3. Purified Water (${self.water_price:.2f})")
        print("4. Exit")
        print(f"Payment Methods: {', '.join(self.payment_methods)}")
        print(f"Current Credit: Hard Cash ${self.hard_cash_credit:.2f}, Virtual ${self.virtual_credit:.2f}")

    def accept_payment(self, price):
        while True:
            try:
                payment_method = input(f"Payment method ({', '.join(self.payment_methods)}): ").lower()
                payment = float(input("Insert payment: $"))
                if payment_method == "cash":
                    self.hard_cash_credit += payment
                    break
                elif payment_method == "virtual":
                    self.virtual_credit += payment
                    break
                else:
                    print("Invalid payment method.")
            except ValueError:
                print("Invalid input. Please enter a number.")

        self.last_interaction_time = datetime.datetime.now()
        self.machine_state = "processing"
        return True

    def dispense_ice(self, ice_type):
        price = self.regular_ice_price if ice_type == "regular" else self.premium_ice_price
        if self.hard_cash_credit + self.virtual_credit >= price:
            try:
                if self.ice_available:
                    print(f"Dispensing {ice_type} ice...")
                    print("Motor 1: Dispensing ice into bag.")
                    print("Motor 2: Delivering bagged ice.")
                    if random.random() < 0.1:
                        raise Exception("Motor malfunction!")
                    if random.random() < 0.1:
                        self.ice_available = False
                        print("WARNING: Low ice detected. Automatic refill initiated.")
                        self.refill_ice()
                    print("Enjoy your ice!")
                    self.sales_data.append({
                        "time": datetime.datetime.now(),
                        "item": f"{ice_type} ice",
                        "price": price
                    })
                    self.ice_dispensed += 1
                    self.process_payment(price)
                    self.reset_credit()
                    self.machine_state = "idle"
                else:
                    raise Exception("Out of ice")
            except Exception as e:
                print(f"Error: {e}")
                self.send_warning(f"Error dispensing ice: {e}")
                self.refund_balance()
                self.machine_state = "out-of-service"
        else:
            print("Insufficient credit.")

    def dispense_water(self):
        if self.hard_cash_credit + self.virtual_credit >= self.water_price:
            try:
                print("Dispensing purified water...")
                print("Motor 3: Dispensing water.")
                if random.random() < 0.1:
                    raise Exception("Water valve malfunction!")
                print("Enjoy your water!")
                self.sales_data.append({
                    "time": datetime.datetime.now(),
                    "item": "Purified water",
                    "price": self.water_price
                })
                self.process_payment(self.water_price)
                self.reset_credit()
                self.machine_state = "idle"
            except Exception as e:
                print(f"Error: {e}")
                self.send_warning(f"Error dispensing water: {e}")
                self.refund_balance()
                self.machine_state = "out-of-service"
        else:
            print("Insufficient credit.")

    def process_payment(self, price):
        if self.hard_cash_credit >= price:
            self.hard_cash_total += price
            self.hard_cash_credit -= price
        else:
            remaining = price - self.hard_cash_credit
            self.hard_cash_total += self.hard_cash_credit
            self.virtual_total += remaining
            self.virtual_credit -= remaining
            self.hard_cash_credit = 0

    def reset_credit(self):
        self.refund_balance()
        self.hard_cash_credit = 0.0
        self.virtual_credit = 0.0

    def refund_balance(self):
        total_refund = self.hard_cash_credit + self.virtual_credit
        print(f"Refunding ${total_refund:.2f}")
        self.hard_cash_credit = 0
        self.virtual_credit = 0

    def refill_ice(self):
        print("Refilling ice...")
        time.sleep(2)
        self.ice_available = True
        print("Refill complete.")

    def send_warning(self, message):
        if self.warning_email:
            print(f"Sending email warning to: {self.warning_email} - {message}")
            # Simulate email sending (replace with actual email sending logic)
        if self.warning_sms:
            print(f"Sending SMS warning to: {self.warning_sms} - {message}")
            # Simulate SMS sending (replace with actual SMS sending logic)

    def check_timeout(self):
        if self.machine_state == "processing" and self.last_interaction_time:
            time_elapsed = (datetime.datetime.now() - self.last_interaction_time).total_seconds()
            if time_elapsed > self.timeout_duration:
                print("Transaction timed out. Returning payment.")
                self.refund_balance()
                self.machine_state = "idle"

    def get_status_report(self):
        print("--- Machine Status ---")
        print(f"State: {self.machine_state}")
        print(f"Balance: Hard Cash ${self.hard_cash_credit:.2f}, Virtual ${self.virtual_credit:.2f}")
        print(f"Ice Available: {self.ice_available}")
        print(f"Ice Dispensed: {self.ice_dispensed}")
        print("--- Sales Data ---")
        for sale in self.sales_data:
            print(f"{sale['time']}: {sale['item']} - ${sale['price']:.2f}")
        print(f"Hard Cash Total: ${self.hard_cash_total:.2f}")
        print(f"Virtual Total: ${self.virtual_total:.2f}")

def run(self):
    while True:
        self.check_timeout()
        if self.machine_state == "idle":
            self.display_menu()
            choice = input("Enter your choice: ")
            match choice:
                case "1":
                    if self.accept_payment(self.regular_ice_price):
                        self.dispense_ice("regular")
                case "2":
                    if self.accept_payment(self.premium_ice_price):
                        self.dispense_ice("premium")
                case "3":
                    if self.accept_payment(self.water_price):
                        self.dispense_water()
                case "4":
                    print("Thank you!")
                    break
                case "status":
                    self.get_status_report()
                case _:
                    print("Invalid choice.")
        elif self.machine_state == "out-of-service":
            print("Machine is out of service. Please contact support.")
            input("Press enter to check status...")
            self.get_status_report()
            if input("Try to restart? (y/n)") == "y":
                self.machine_state = "idle"