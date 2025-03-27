# tkinter_ui.py
import tkinter as tk
import json
from controller.vmc import VMC

class VendingMachineUI:
    def __init__(self, root, config_file='config.json'):
        self.root = root
        self.vmc = VMC(config_file=config_file)
        # Set the VMC update callback to update the UI status label
        self.vmc.set_update_callback(self.update_status)
        self.create_widgets()

    def create_widgets(self):
        # Create a frame to display configuration info
        self.info_frame = tk.Frame(self.root)
        self.info_frame.pack(pady=10)

        # Load configuration to display product info and owner contact
        with open('config.json', 'r') as f:
            config = json.load(f)
        products = config.get("products", [])
        owner_contact = config.get("owner_contact", {})

        # Display products info
        self.products_label = tk.Label(self.info_frame, text="Products:")
        self.products_label.pack()

        self.product_list = tk.Listbox(self.info_frame, width=50)
        for i, product in enumerate(products):
            self.product_list.insert(tk.END, f"{i}: {product.get('name')} - ${product.get('price'):.2f}")
        self.product_list.pack()

        # Display owner contact info
        self.owner_label = tk.Label(
            self.info_frame,
            text=f"Owner Contact: Email: {owner_contact.get('email', '')}, SMS: {owner_contact.get('sms', '')}"
        )
        self.owner_label.pack()

        # Create a frame for product buttons
        self.button_frame = tk.Frame(self.root)
        self.button_frame.pack(pady=10)

        self.buttons = []
        for i, product in enumerate(products):
            btn = tk.Button(self.button_frame, text=product.get('name'),
                            command=lambda idx=i: self.product_pressed(idx))
            btn.pack(side=tk.LEFT, padx=5)
            self.buttons.append(btn)

        # Label for FSM state
        self.state_label = tk.Label(self.root, text="Current State: idle")
        self.state_label.pack(pady=10)

    def product_pressed(self, index):
        # When a product button is pressed, call the VMC's select_product method.
        self.vmc.select_product(index, self.root)

    def update_status(self, state, selected_product):
        product_name = selected_product.get('name') if selected_product else "None"
        self.state_label.config(text=f"Current State: {state}. Selected Product: {product_name}")

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Vending Machine Controller")
    app = VendingMachineUI(root)
    root.mainloop()
