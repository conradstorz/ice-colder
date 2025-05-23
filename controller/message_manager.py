import tkinter as tk
from abc import ABC, abstractmethod
from collections import deque

class DisplayDevice(ABC):
    """
    Abstract interface for all display devices.
    """
    @abstractmethod
    def show_text(self, message: str):
        """Render the given text message on the device."""
        pass

    @abstractmethod
    def clear(self):
        """Clear the display."""
        pass

    @abstractmethod
    def schedule(self, delay_ms: int, callback) -> str:
        """Schedule a callback after delay_ms milliseconds; return a timer ID."""
        pass

    @abstractmethod
    def cancel(self, timer_id: str):
        """Cancel a previously scheduled callback."""
        pass

class TkinterWindowDisplay(DisplayDevice):
    """
    A simple Tkinter window that simulates a display device for text.
    Supports positioning relative to an optional parent window.
    """
    def __init__(self, parent_window=None, width=400, height=100, x_offset=50, y_offset=50):
        # Create a standalone or child window
        if parent_window:
            self.root = tk.Toplevel(master=parent_window)
            # Position window relative to parent to avoid overlap
            parent_window.update_idletasks()
            px = parent_window.winfo_x()
            py = parent_window.winfo_y()
            self.root.geometry(f"{width}x{height}+{px + x_offset}+{py + y_offset}")
        else:
            self.root = tk.Toplevel()
            self.root.geometry(f"{width}x{height}")
        self.root.title("Display Manager")
        # Label for showing text; supports multiline
        self.label = tk.Label(self.root, text="", font=("Helvetica", 14), justify="left")
        self.label.pack(expand=True, fill="both")

    def show_text(self, message: str):
        self.label.config(text=message)

    def clear(self):
        self.label.config(text="")

    def schedule(self, delay_ms: int, callback) -> str:
        # Use Tkinter's after to schedule
        timer_id = self.root.after(delay_ms, callback)
        return timer_id

    def cancel(self, timer_id: str):
        # Cancel a pending after callback
        self.root.after_cancel(timer_id)

class MessageManager:
    """
    Queues messages for sequential display, ensuring each is shown for its full duration.
    """
    def __init__(self, device: DisplayDevice):
        self.device = device
        self.queue = deque()
        self.current_timer = None

    def post(self, message: str, duration_ms: int = 5000):
        """
        Enqueue a message; if idle, start displaying immediately.
        """
        self.queue.append((message, duration_ms))
        if len(self.queue) == 1:
            self._display_next()

    def _display_next(self):
        if not self.queue:
            self.device.clear()
            return
        message, duration = self.queue[0]
        self.device.show_text(message)
        # Schedule removal of this message
        self.current_timer = self.device.schedule(duration, self._on_timeout)

    def _on_timeout(self):
        # Remove the message that just expired
        self.queue.popleft()
        # Display next or clear
        self._display_next()

    def clear_all(self):
        """
        Cancel pending messages and clear display.
        """
        if self.current_timer:
            self.device.cancel(self.current_timer)
        self.queue.clear()
        self.device.clear()
