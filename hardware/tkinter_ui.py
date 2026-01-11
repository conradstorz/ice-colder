# tkinter_ui.py
import tkinter as tk
from functools import partial  # For binding callbacks with parameters
from tkinter import ttk  # Import ttk for Notebook widget

from PIL import ImageTk  # Import Pillow for image handling

from config.config_model import ConfigModel  # Import Pydantic model for configuration
from controller.message_manager import MessageManager, TkinterWindowDisplay
from controller.vmc import VMC  # Import the VMC class from the controller module


class VendingMachineUI:
    def __init__(self, root, config_model: ConfigModel):
        self.root = root
        self.config_model = config_model

        # Initialize the message manager before setting callbacks
        self.display_manager = MessageManager(TkinterWindowDisplay(parent_window=self.root, x_offset=450, y_offset=0))

        # Initialize VMC with pre-loaded Pydantic ConfigModel
        self.vmc = VMC(config=self.config_model)

        # Set the VMC update callback to update the UI status label
        self.vmc.set_update_callback(self.update_status)

        # Set the VMC message callback to show messages via the display manager
        self.vmc.set_message_callback(partial(self.display_manager.post, duration_ms=5000))

        # Set the VMC QR code callback to update the QR code display area
        self.vmc.set_qrcode_callback(self.update_qrcode)

        self.create_widgets()

    def create_widgets(self):
        # Set Notebook style to enlarge tabs (approximately 3x larger)
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=(20, 10), font=("Helvetica", 16))

        # Load data from Pydantic ConfigModel
        phys_details = self.config_model.physical_details
        products = phys_details.products

        # Machine owner contact information
        machine_owner = self.config_model.people.machine_owner
        owner_info = f"Owner: {machine_owner.name}\nPhone: {machine_owner.phone_number}\nEmail: {machine_owner.email}"

        # Location owner contact information (available if needed)
        # loc_owner = self.config_model.people.location_owner

        # Physical location details
        phys_loc = phys_details.location
        machine_id = phys_details.machine_id
        location_str = f"Address: {phys_loc.address}\nNotes: {phys_loc.notes}"

        # Repair service details (not in ConfigModel, placeholders)
        repair_info = "Repair Service: N/A\nPhone: N/A\nEmail: N/A"

        # Create a Notebook widget for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(expand=True, fill="both")

        # ---------------------------
        # Tab 1: Info Tab (Machine & Owner Info)
        # ---------------------------
        self.info_tab = tk.Frame(self.notebook)
        self.notebook.add(self.info_tab, text="Info")

        self.top_info_frame = tk.Frame(self.info_tab)
        self.top_info_frame.pack(pady=5)

        # Machine Information
        self.machine_info_frame = tk.Frame(self.top_info_frame)
        self.machine_info_frame.pack(side=tk.LEFT, padx=10)
        self.machine_id_label = tk.Label(
            self.machine_info_frame, text=f"Machine ID: {machine_id}", font=("Helvetica", 12, "bold")
        )
        self.machine_id_label.pack()
        self.location_label = tk.Label(self.machine_info_frame, text=location_str, font=("Helvetica", 10))
        self.location_label.pack()

        # Owner Information
        self.owner_info_frame = tk.Frame(self.top_info_frame)
        self.owner_info_frame.pack(side=tk.LEFT, padx=10)
        self.owner_info_label = tk.Label(self.owner_info_frame, text=owner_info, font=("Helvetica", 10))
        self.owner_info_label.pack()

        # ---------------------------
        # Tab 2: Products Tab (Product List & Details)
        # ---------------------------
        self.products_tab = tk.Frame(self.notebook)
        self.notebook.add(self.products_tab, text="Products")

        self.prod_info_frame = tk.Frame(self.products_tab)
        self.prod_info_frame.pack(pady=10)
        self.products_label = tk.Label(self.prod_info_frame, text="Products:")
        self.products_label.pack()

        self.product_list = tk.Listbox(self.prod_info_frame, width=50)
        for i, product in enumerate(products):
            track_inv = getattr(product, "track_inventory", False)
            inv_count = getattr(product, "inventory_count", 0)
            name = product.name
            price = product.price
            inventory_text = f"({inv_count} available)" if track_inv else "(Unlimited)"
            self.product_list.insert(
                tk.END,
                f"{i}: {name} - ${price:.2f} {inventory_text}",
            )
        self.product_list.pack()

        # ---------------------------
        # Tab 3: Control Tab (Payment Simulation, Refund, FSM State, Messages, QR Code Display)
        # ---------------------------
        self.control_tab = tk.Frame(self.notebook)
        self.notebook.add(self.control_tab, text="Control")

        self.escrow_label = tk.Label(self.control_tab, text="Money In: $0.00", font=("Helvetica", 14, "bold"))
        self.escrow_label.pack(pady=5)

        # Product buttons
        self.button_frame = tk.Frame(self.control_tab)
        self.button_frame.pack(pady=10)
        self.buttons = []
        for i, product in enumerate(products):
            btn = tk.Button(
                self.button_frame,
                text=product.name,
                command=lambda idx=i: self.product_pressed(idx),
            )
            btn.pack(side=tk.LEFT, padx=5)
            self.buttons.append(btn)

        # Payment simulation and refund
        self.payment_frame = tk.Frame(self.control_tab)
        self.payment_frame.pack(pady=10)
        self.payment_label = tk.Label(self.payment_frame, text="Simulate Payment:")
        self.payment_label.pack(side=tk.LEFT, padx=5)

        self.denominations = [0.05, 0.10, 0.25, 0.50, 1, 5, 10, 20]
        for amount in self.denominations:
            btn = tk.Button(
                self.payment_frame,
                text=f"${amount:.2f}",
                command=lambda amt=amount: self.simulate_payment(amt),
            )
            btn.pack(side=tk.LEFT, padx=3)

        self.refund_button = tk.Button(self.payment_frame, text="Request Refund", command=self.request_refund)
        self.refund_button.pack(side=tk.LEFT, padx=5)

        # FSM state and selected product
        self.state_label = tk.Label(
            self.control_tab, text="Current State: idle\nSelected Product: None", justify="left", font=("Helvetica", 12)
        )
        self.state_label.pack(pady=10)

        # Message display
        self.message_text = tk.Text(self.control_tab, height=4, width=60, wrap="word")
        self.message_text.pack(pady=10)
        self.message_text.config(state="disabled")

        # QR code display
        self.qrcode_label = tk.Label(self.control_tab)
        self.qrcode_label.pack(pady=10)

        # ---------------------------
        # Tab 4: Repair Tab (Repair Service Info)
        # ---------------------------
        self.repair_tab = tk.Frame(self.notebook)
        self.notebook.add(self.repair_tab, text="Repair Service")
        self.repair_info_label = tk.Label(self.repair_tab, text=repair_info, font=("Helvetica", 10))
        self.repair_info_label.pack(padx=10, pady=10)

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
        # Update the state label with current FSM state and selected product
        product_name = selected_product.name if selected_product else "None"
        self.state_label.config(text=f"Current State: {state}\nSelected Product: {product_name}")

    def update_message(self, message):
        # Enable, clear, write, and disable the text widget
        self.message_text.config(state="normal")
        self.message_text.delete("1.0", tk.END)
        self.message_text.insert(tk.END, message)
        self.message_text.config(state="disabled")

    def update_qrcode(self, pil_image):
        # Convert the PIL image to a Tkinter PhotoImage and update the QR code label
        self.qr_photo = ImageTk.PhotoImage(pil_image)
        self.qrcode_label.config(image=self.qr_photo)
        self.qrcode_label.image = self.qr_photo


if __name__ == "__main__":
    root = tk.Tk()
    root.title("Vending Machine Controller")
    # Load Pydantic config externally and pass it here
    with open("config.json", encoding="utf-8") as f:
        config_data = f.read()
    config = ConfigModel.model_validate_json(config_data)
    app = VendingMachineUI(root, config_model=config)
    root.mainloop()
# End of fixed tkinter UI module
