import tkinter as tk
from tkinter import scrolledtext, messagebox
import requests
import re
from bs4 import BeautifulSoup
import threading

class EmailDiscoveryTool:
    def __init__(self, root):
        self.root = root
        self.root.title("Email Discovery Tool")
        self.root.geometry("600x450")

        # UI Elements
        # Label changed from "Data Analysis" context to "Email Discovery"
        self.label_url = tk.Label(root, text="Target Website URL (e.g., https://example.com):", font=("Arial", 12))
        self.label_url.pack(pady=10)

        self.entry_url = tk.Entry(root, width=50, font=("Arial", 12))
        self.entry_url.pack(pady=5)

        self.btn_analyze = tk.Button(root, text="Start Email Discovery", command=self.start_discovery_thread, 
                                     bg="#4CAF50", fg="white", font=("Arial", 12, "bold"))
        self.btn_analyze.pack(pady=15)

        self.result_label = tk.Label(root, text="Discovery Results:", font=("Arial", 12))
        self.result_label.pack(pady=5)

        self.text_area = scrolledtext.ScrolledText(root, width=70, height=15, font=("Consolas", 10))
        self.text_area.pack(pady=10)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_var.set("Ready")
        self.status_bar = tk.Label(root, textvariable=self.status_var, bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def start_discovery_thread(self):
        """Runs the scraping in a separate thread to prevent UI freezing."""
        url = self.entry_url.get().strip()
        if not url:
            messagebox.showwarning("Input Error", "Please enter a valid URL.")
            return
        
        # Add protocol if missing
        if not url.startswith("http"):
            url = "https://" + url

        self.btn_analyze.config(state=tk.DISABLED)
        self.text_area.delete('1.0', tk.END)
        self.status_var.set(f"Scanning {url}...")
        
        # Threading
        t = threading.Thread(target=self.perform_discovery, args=(url,))
        t.daemon = True
        t.start()

    def perform_discovery(self, url):
        """The core logic for finding emails."""
        emails_found = set()
        
        try:
            # IMPROVEMENT: Add User-Agent headers to look like a real browser
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')

            # Method 1: Regex search on the raw text
            # IMPROVEMENT: refined regex to capture standard emails but avoid common false positives
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            text_emails = re.findall(email_pattern, response.text)
            for email in text_emails:
                emails_found.add(email.lower())

            # Method 2: Specifically look for 'mailto:' links
            # IMPROVEMENT: This catches emails hidden in buttons/links that regex on raw text might miss
            for link in soup.find_all('a', href=True):
                if link['href'].startswith('mailto:'):
                    email = link['href'].replace('mailto:', '').split('?')[0]
                    emails_found.add(email.lower())

            # Filter out common junk image extensions if they got caught by regex
            junk_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.css', '.js')
            clean_emails = {e for e in emails_found if not e.endswith(junk_extensions)}

            self.update_ui_results(clean_emails)

        except requests.exceptions.RequestException as e:
            self.update_ui_error(f"Network Error: {str(e)}")
        except Exception as e:
            self.update_ui_error(f"An error occurred: {str(e)}")

    def update_ui_results(self, emails):
        """Updates the text area with found emails."""
        self.root.after(0, lambda: self._update_results_safe(emails))

    def _update_results_safe(self, emails):
        if emails:
            result_text = "Found {} unique email(s):\n\n".format(len(emails))
            result_text += "\n".join(sorted(emails))
        else:
            result_text = "No emails found on this page.\nTip: Try checking the 'Contact' or 'About Us' page specific URLs."
        
        self.text_area.insert(tk.END, result_text)
        self.status_var.set("Discovery Complete.")
        self.btn_analyze.config(state=tk.NORMAL)

    def update_ui_error(self, message):
        self.root.after(0, lambda: self._show_error_safe(message))

    def _show_error_safe(self, message):
        self.text_area.insert(tk.END, message)
        self.status_var.set("Error encountered.")
        self.btn_analyze.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = EmailDiscoveryTool(root)
    root.mainloop()