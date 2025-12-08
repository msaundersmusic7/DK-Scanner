from flask import Flask, render_template_string, request
import requests
import re
from bs4 import BeautifulSoup

app = Flask(__name__)

# HTML Template for the Web Interface (styled to look clean)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Discovery Tool</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f4f4f9; padding-top: 50px; }
        .container { max-width: 700px; background: white; padding: 30px; border-radius: 10px; shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .result-box { background: #1e1e1e; color: #00ff00; padding: 15px; border-radius: 5px; font-family: monospace; min-height: 150px; }
    </style>
</head>
<body>
    <div class="container shadow">
        <h2 class="text-center mb-4">Email Discovery Tool</h2>
        <form method="POST">
            <div class="mb-3">
                <label for="url" class="form-label">Target Website URL:</label>
                <input type="text" class="form-control" id="url" name="url" placeholder="example.com" required value="{{ url }}">
            </div>
            <button type="submit" class="btn btn-success w-100">Start Discovery</button>
        </form>

        {% if results is not none %}
        <div class="mt-4">
            <h4>Results:</h4>
            <div class="result-box">
                {% if error %}
                    <span style="color: #ff4444;">{{ error }}</span>
                {% elif results %}
                    Found {{ results|length }} unique email(s):<br><br>
                    {% for email in results %}
                        {{ email }}<br>
                    {% endfor %}
                {% else %}
                    No emails found on this page.<br>
                    Tip: Try checking the 'Contact' or 'About Us' page specific URLs.
                {% endif %}
            </div>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

def perform_discovery(url):
    """Core logic extracted from the old Tkinter app."""
    emails_found = set()
    try:
        if not url.startswith("http"):
            url = "https://" + url

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')

        # Method 1: Regex
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        text_emails = re.findall(email_pattern, response.text)
        for email in text_emails:
            emails_found.add(email.lower())

        # Method 2: Mailto links
        for link in soup.find_all('a', href=True):
            if link['href'].startswith('mailto:'):
                email = link['href'].replace('mailto:', '').split('?')[0]
                emails_found.add(email.lower())

        # Filter junk
        junk_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.css', '.js')
        clean_emails = {e for e in emails_found if not e.endswith(junk_extensions)}
        
        return sorted(list(clean_emails)), None

    except Exception as e:
        return [], str(e)

@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    error = None
    url = ""
    
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        if url:
            results, error = perform_discovery(url)
            
    return render_template_string(HTML_TEMPLATE, results=results, error=error, url=url)

if __name__ == "__main__":
    app.run(debug=True)