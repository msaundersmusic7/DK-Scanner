import os
import base64
import time
from flask import Flask, jsonify, make_response
from flask_cors import CORS
import requests
import logging
# Import modules needed for parsing URLs
from urllib.parse import urlparse, parse_qs

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

    # *** THIS IS THE CORRECT URL ***
    auth_url = 'https://accounts.spotify.com/api/token'
    
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

# --- Helper Function: Get Full Album Details ---
def get_full_album_details(album_ids, token):
    """
    Fetches full album details for a list of album IDs.
    The 'search' endpoint only returns simplified objects without copyright info.
    This function gets the full objects.
    """
    if not album_ids:
        return []
        
    # *** THIS IS THE CORRECTED URL (was 'https' before) ***
    albums_url = 'https://api.spotify.com/v1/albums' 
    
    full_album_list = []
    
    # Split album_ids into chunks of 20 (Spotify's max for this endpoint)
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i + 20]
        
        params = {
            'ids': ','.join(chunk) # Spotify API takes a comma-separated string of IDs
        }
        auth_header = {"Authorization": f"Bearer {token}"}
        
        try:
            response = requests.get(albums_url, headers=auth_header, params=params)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited getting album details. Waiting {retry_after}s...")
                time.sleep(retry_after)
                # Re-run this chunk
                response = requests.get(albums_url, headers=auth_header, params=params)

            response.raise_for_status()
            data = response.json()
            full_album_list.extend(data.get('albums', []))
        
        except requests.exceptions.HTTPError as err:
            app.logger.error(f"HTTP error getting full album details: {err}")
            # Continue to the next chunk
        except Exception as e:
            app.logger.error(f"Unexpected error in get_full_album_details: {e}")
            # Continue to the next chunk
            
    return full_album_list


# --- Main API Route: /api/scan ---
@app.route('/api/scan')
def scan_for_artists():
    """
    Main API endpoint to scan Spotify and return a list of artists.
    """
    app.logger.info("Received scan request at /api/scan")
    token = get_spotify_token()
    
    if not token:
        app.logger.warning("Token request failed. Sending auth error to client.")
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    app.logger.info("Authentication successful. Starting server-side scan...")
    artists_found = {} # Use a dictionary to store {name: url}
    total_albums_scanned = 0
    
    # Start with the base search URL
    search_url = 'https://api.spotify.com/v1/search'
    auth_header = {"Authorization": f"Bearer {token}"}
    
    page_count = 0
    max_pages = 20 # Limit to 20 pages (1000 albums)
    
    # Set initial parameters for the first request
    params = {
        'q': 'label:"Records DK"',  # *** THE CORRECT, TARGETED SEARCH QUERY ***
        'type': 'album',
        'limit': 50,       # Get 50 albums per page
        'offset': 0
    }
    
    next_url = search_url # For the first loop
    
    while next_url and page_count < max_pages:
        try:
            # 1. SEARCH FOR ALBUMS (Simplified)
            app.logger.debug(f"Scanning page {page_count + 1}...")
            if page_count == 0:
                response = requests.get(search_url, headers=auth_header, params=params)
            else:
                response = requests.get(next_url, headers=auth_header)

            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            data = response.json()
            simplified_albums = data.get('albums', {}).get('items', [])
            
            if not simplified_albums:
                app.logger.info("No more albums found.")
                break 

            # Collect all album IDs from this page
            album_ids = [album['id'] for album in simplified_albums if album and album.get('id')]
            if not album_ids:
                app.logger.info("Found albums, but no IDs. Moving to next page.")
                next_url = data.get('albums', {}).get('next')
                page_count += 1
                continue

            # 2. GET FULL ALBUM DETAILS (with Copyrights)
            app.logger.debug(f"Getting full details for {len(album_ids)} albums...")
            full_albums = get_full_album_details(album_ids, token)
            
            total_albums_scanned += len(full_albums)
            page_count += 1

            # 3. FILTER THE FULL ALBUM DETAILS
            for album in full_albums:
                if not album: continue
                for copyright in album.get('copyrights', []):
                    copyright_text = copyright.get('text', '').lower()
                    
                    # *** FINAL, CORRECTED FILTER ***
                    # Check for "records dk" (as requested) OR "distrokid" in the P-line
                    if copyright.get('type') == 'P' and ('records dk' in copyright_text or 'distrokid' in copyright_text):
                        for artist in album.get('artists', []):
                            artist_name = artist.get('name')
                            artist_url = artist.get('external_urls', {}).get('spotify')
                            if artist_name and artist_name not in artists_found:
                                artists_found[artist_name] = artist_url
                        break # Move to the next album
            
            app.logger.info(f"Scanned {total_albums_scanned} albums... Found {len(artists_found)} unique artists so far.")
            
            # Get the URL for the next page of results
            next_url = data.get('albums', {}).get('next')
            
            time.sleep(0.1) # Be nice to the API

        except requests.exceptions.HTTPError as err:
            app.logger.error(f"ERROR during search: {err}")
            return jsonify({"error": f"Error during Spotify search: {err.response.text}"}), 500
        except Exception as e:
            app.logger.error(f"ERROR: An unexpected error occurred during search: {e}")
            return jsonify({"error": f"An unexpected error occurred: {e}"}), 500

    app.logger.info(f"Scan complete. Found {len(artists_found)} artists.")
    
    # Convert the dictionary to the list of objects for the frontend
    artist_list = [{"name": name, "url": url} for name, url in artists_found.items()]
    # Sort the list alphabetically by artist name
    artist_list_sorted = sorted(artist_list, key=lambda k: k['name'])
    
    return jsonify({"artists": artist_list_sorted})

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