# tkinter_ui.py
import tkinter as tk
import json
from controller.vmc import VMC


class VendingMachineUI:
    def __init__(self, root, config_file="config.json"):
        self.root = root
        self.vmc = VMC(config_file=config_file)
        # Set the VMC update callback to update the UI status label
        self.vmc.set_update_callback(self.update_status)
        # Set the VMC message callback to update the message area
        self.vmc.set_message_callback(self.update_message)
        self.create_widgets(config_file)

    def create_widgets(self, config_file):
        # Load configuration to retrieve machine and owner info, and product details
        with open(config_file, "r") as f:
            config = json.load(f)
        products = config.get("products", [])
        # Get owner info from ownership_details if available, otherwise use owner_contact
        ownership_details = config.get("ownership_details", {})
        if ownership_details:
            owner_info = (f"Owner: {ownership_details.get('owner_name', 'N/A')}\n"
                          f"Phone: {ownership_details.get('contact_info', {}).get('phone_number', 'N/A')}\n"
                          f"Email: {ownership_details.get('contact_info', {}).get('email_address', 'N/A')}")
        else:
            owner_contact = config.get("owner_contact", {})
            owner_info = (f"Owner Email: {owner_contact.get('email', 'N/A')}\n"
                          f"Owner SMS: {owner_contact.get('sms', 'N/A')}")
        machine_id = config.get("machine_id", "N/A")
        location = config.get("location", {})
        location_str = (
            f"{location.get('address', 'Unknown')}\n"
            f"Type: {location.get('type', 'Unknown')}, Area: {location.get('placement_area', 'Unknown')}\n"
            f"Traffic Level: {location.get('traffic_level', 'Unknown')}"
        )

        # Create a top-level frame for machine info and owner info, placed side by side
        self.top_info_frame = tk.Frame(self.root)
        self.top_info_frame.pack(pady=5)

        # Create a frame for Machine Information on the left
        self.machine_info_frame = tk.Frame(self.top_info_frame)
        self.machine_info_frame.pack(side=tk.LEFT, padx=10)
        self.machine_id_label = tk.Label(self.machine_info_frame, text=f"Machine ID: {machine_id}", font=("Helvetica", 12, "bold"))
        self.machine_id_label.pack()
        self.location_label = tk.Label(self.machine_info_frame, text=f"Location:\n{location_str}", font=("Helvetica", 10))
        self.location_label.pack()

        # Create a frame for Owner Information on the right
        self.owner_info_frame = tk.Frame(self.top_info_frame)
        self.owner_info_frame.pack(side=tk.LEFT, padx=10)
        self.owner_info_label = tk.Label(self.owner_info_frame, text=owner_info, font=("Helvetica", 10))
        self.owner_info_label.pack()

        # Create a label at the top to display the "Money In" escrow balance
        self.escrow_label = tk.Label(self.root, text="Money In: $0.00", font=("Helvetica", 14, "bold"))
        self.escrow_label.pack(pady=5)

        # Create a frame to display configuration info (products and owner contact details)
        self.info_frame = tk.Frame(self.root)
        self.info_frame.pack(pady=10)

        # Display products info
        self.products_label = tk.Label(self.info_frame, text="Products:")
        self.products_label.pack()

        self.product_list = tk.Listbox(self.info_frame, width=50)
        for i, product in enumerate(products):
            if product.get("track_inventory", False):
                inventory_text = f"({product.get('inventory_count', 0)} available)"
            else:
                inventory_text = "(Unlimited)"
            self.product_list.insert(
                tk.END,
                f"{i}: {product.get('name')} - ${product.get('price'):.2f} {inventory_text}",
            )
        self.product_list.pack()

        # Display owner contact info (redundant info, but preserved as per original design)
        self.owner_label = tk.Label(
            self.info_frame,
            text=f"Owner Contact: Email: {config.get('owner_contact', {}).get('email', 'N/A')}, SMS: {config.get('owner_contact', {}).get('sms', 'N/A')}",
        )
        self.owner_label.pack()

        # Create a frame for product buttons
        self.button_frame = tk.Frame(self.root)
        self.button_frame.pack(pady=10)

        self.buttons = []
        for i, product in enumerate(products):
            btn = tk.Button(
                self.button_frame,
                text=product.get("name"),
                command=lambda idx=i: self.product_pressed(idx),
            )
            btn.pack(side=tk.LEFT, padx=5)
            self.buttons.append(btn)

        # Create a frame for simulating payment (coins and paper bills) and refund button
        self.payment_frame = tk.Frame(self.root)
        self.payment_frame.pack(pady=10)

        self.payment_label = tk.Label(self.payment_frame, text="Simulate Payment:")
        self.payment_label.pack(side=tk.LEFT, padx=5)

        # Denominations for coins and bills up to $20
        self.denominations = [0.05, 0.10, 0.25, 0.50, 1, 5, 10, 20]
        for amount in self.denominations:
            btn = tk.Button(
                self.payment_frame,
                text=f"${amount:.2f}",
                command=lambda amt=amount: self.simulate_payment(amt),
            )
            btn.pack(side=tk.LEFT, padx=3)

        # Add a new button for requesting a refund of unused credit
        self.refund_button = tk.Button(self.payment_frame, text="Request Refund", command=self.request_refund)
        self.refund_button.pack(side=tk.LEFT, padx=5)

        # Create a label for displaying FSM state and selected product on separate lines
        self.state_label = tk.Label(self.root, text="Current State: idle\nSelected Product: None", justify="left", font=("Helvetica", 12))
        self.state_label.pack(pady=10)

        # Create a Text widget for messages so that each message appears on its own line
        self.message_text = tk.Text(self.root, height=4, width=60, wrap="word")
        self.message_text.pack(pady=10)
        # Disable editing by the user
        self.message_text.config(state="disabled")

    def product_pressed(self, index):
        # When a product button is pressed, call the VMC's select_product method.
        self.vmc.select_product(index, self.root)

    def simulate_payment(self, amount):
        # Called when a coin or bill button is pressed; simulate depositing funds.
        self.vmc.deposit_funds(amount, "Simulated Payment")

    def request_refund(self):
        # Called when the "Request Refund" button is pressed.
        self.vmc.request_refund(self.root)

    def update_status(self, state, selected_product, credit_escrow):
        # Update the "Money In" label with the escrow balance
        self.escrow_label.config(text=f"Money In: ${credit_escrow:.2f}")
        # Update the state label with current FSM state and selected product (on separate lines)
        product_name = selected_product.get("name") if selected_product else "None"
        self.state_label.config(
            text=f"Current State: {state}\nSelected Product: {product_name}"
        )

    def update_message(self, message):
        # Enable the text widget to update its content
        self.message_text.config(state="normal")
        # Clear previous messages
        self.message_text.delete("1.0", tk.END)
        # Insert the new message (each new message on its own line if needed)
        self.message_text.insert(tk.END, message)
        # Disable editing again
        self.message_text.config(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    root.title("Vending Machine Controller")
    app = VendingMachineUI(root)
    root.mainloop()

# This code is a simple GUI for a vending machine monitor using Tkinter.
# It displays product information, owner contact details, and allows users to select products.
# The GUI updates the current state of the vending machine and the selected product.
# The VMC class handles the vending machine logic and state management.
# The GUI is initialized with a configuration file that contains product and owner information.
# The product buttons are dynamically created based on the configuration file.
# The GUI also includes a label to display the current state of the vending machine and the selected product.
# The update_status method updates the label with the current state and selected product information.
# The product_pressed method is called when a product button is pressed, which triggers the VMC's select_product method.
# The GUI is run in the main loop, allowing for user interaction.
# The code is designed to be modular and can be easily extended or modified for additional functionality.
# The use of JSON for configuration allows for easy updates to product information without modifying the code.
# The GUI is responsive and provides feedback to the user based on the vending machine's state.
# The code is structured to separate the GUI logic from the vending machine controller logic, promoting clean code practices.
# The use of the loguru library for logging is a good practice for debugging and monitoring the application.
# The code is well-organized and follows Python's PEP 8 style guide for readability.
# Overall, this code provides a solid foundation for a vending machine monitor with a user-friendly interface.
# It can be further enhanced with additional features such as error handling, user authentication, and more advanced state management.
