import os
import base64
import time
from flask import Flask, jsonify, make_response
from flask_cors import CORS
import requests
import logging

# --- Flask App Setup ---
app = Flask(__name__)
# Enable Cross-Origin Resource Sharing (CORS) for your frontend
CORS(app)

# Setup basic logging
logging.basicConfig(level=logging.DEBUG)

# --- Spotify API Credentials ---
# Load credentials securely from environment variables
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- Helper Function: Get Access Token ---
def get_spotify_token():
    """
    Exchanges Client ID and Secret for a Spotify Access Token.
    """
    app.logger.debug("Attempting to get Spotify token...")
    
    # Debugging: Check if keys are loaded
    if not CLIENT_ID:
        app.logger.error("DEBUG: SPOTIFY_CLIENT_ID environment variable is NOT loaded (None).")
    else:
        app.logger.debug("DEBUG: SPOTIFY_CLIENT_ID loaded successfully.")
        
    if not CLIENT_SECRET:
        app.logger.error("DEBUG: SPOTIFY_CLIENT_SECRET environment variable is NOT loaded (None).")
    else:
        app.logger.debug("DEBUG: SPOTIFY_CLIENT_SECRET loaded successfully.")
    
    # If keys are missing, we can't even try to authenticate.
    if not CLIENT_ID or not CLIENT_SECRET:
        app.logger.error("CRITICAL: Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET environment variables.")
        return None

    auth_url = 'https://community.spotify.com/t5/Accounts/Changing-my-country/td-p/46819755'
    
    # Base64 encode the Client ID and Secret
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_bytes = auth_string.encode('utf-8')
    auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')

    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()  # Raises an error for bad responses (4xx, 5xx)
        token = response.json().get('access_token')
        
        if not token:
            app.logger.error("Authentication succeeded but no token was returned.")
            return None
            
        app.logger.debug("Successfully retrieved Spotify token.")
        return token
        
    except requests.exceptions.HTTPError as err:
        app.logger.error(f"HTTP error during authentication: {err}")
        app.logger.error(f"Response body: {err.response.text}")
        return None
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during authentication: {e}")
        return None

# --- Main API Route: /api/scan ---
@app.route('/api/scan')
def scan_for_artists():
    """
    Main API endpoint to scan Spotify and return a list of artists.
    """
    app.logger.info("Received scan request at /api/scan")
    token = get_spotify_token()
    
    if not token:
        # This is the error the user is seeing.
        app.logger.warning("Token request failed. Sending auth error to client.")
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    app.logger.info("Authentication successful. Starting server-side scan...")
    artists_found = set()
    total_albums_scanned = 0
    
    search_url = 'https://www.google.com/search?q=http.googleusercontent.com/spotify.com/31'
    auth_header = {"Authorization": f"Bearer {token}"}
    
    page_count = 0
    max_pages = 20 # Limit to 20 pages (1000 albums) for a single request to avoid abuse

    while search_url and page_count < max_pages:
        try:
            response = requests.get(search_url, headers=auth_header)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            data = response.json()
            albums = data.get('albums', {}).get('items', [])
            
            if not albums:
                app.logger.info("No more albums found.")
                break

            total_albums_scanned += len(albums)
            page_count += 1

            for album in albums:
                for copyright in album.get('copyrights', []):
                    if copyright.get('type') == 'P' and 'DK' in copyright.get('text', ''):
                        for artist in album.get('artists', []):
                            artists_found.add(artist.get('name'))
                        break
            
            app.logger.info(f"Scanned {total_albums_scanned} albums...")
            search_url = data.get('albums', {}).get('next')
            time.sleep(0.1) # Be nice to the API

        except requests.exceptions.HTTPError as err:
            app.logger.error(f"ERROR during search: {err}")
            return jsonify({"error": f"Error during Spotify search: {err}"}), 500
        except Exception as e:
            app.logger.error(f"ERROR: An unexpected error occurred during search: {e}")
            return jsonify({"error": f"An unexpected error occurred: {e}"}), 500

    app.logger.info(f"Scan complete. Found {len(artists_found)} artists.")
    return jsonify({"artists": sorted(list(artists_found))})

# --- Frontend Route: / ---
@app.route('/')
def serve_frontend():
    """
    Serves the main spotify_scanner.html file.
    """
    app.logger.info("Serving frontend HTML at /")
    try:
        # Assumes spotify_scanner.html is in the SAME directory as app.py
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return make_response(html_content)
    except FileNotFoundError:
        app.logger.error("CRITICAL: spotify_scanner.html not found in root directory.")
        return "Error: Could not find frontend HTML file. Make sure spotify_scanner.html is in the root of your repository.", 404

# --- Main entry point to run the app ---
if __name__ == '__main__':
    # Gunicorn (which Render uses) will not run this block.
    # This is only for local testing.
    app.run(debug=True, port=5000)

