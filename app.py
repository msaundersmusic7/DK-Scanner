import os
import base64
import time
import random
import string
import re
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
    """
    if not album_ids:
        return []
        
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


# --- Helper Function to Get Artist Details ** ---
def get_artist_details(artist_ids, token):
    """
    Fetches full artist details (followers, popularity) for a list of artist IDs.
    """
    if not artist_ids:
        return []

    artists_url = 'https://api.spotify.com/v1/artists'
    auth_header = {"Authorization": f"Bearer {token}"}
    
    artist_details_list = []

    # Split artist_ids into chunks of 50 (Spotify's max for this endpoint)
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i + 50]
        params = {'ids': ','.join(chunk)}
        
        try:
            response = requests.get(artists_url, headers=auth_header, params=params)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited getting artist details. Waiting {retry_after}s...")
                time.sleep(retry_after)
                # Re-run this chunk
                response = requests.get(artists_url, headers=auth_header, params=params)

            response.raise_for_status()
            data = response.json()
            artist_details_list.extend(data.get('artists', []))
        
        except requests.exceptions.HTTPError as err:
            app.logger.error(f"HTTP error getting artist details: {err}")
        except Exception as e:
            app.logger.error(f"Unexpected error in get_artist_details: {e}")

    return artist_details_list


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
    artists_found = {} # Use a dictionary to store {id: {name, url}}
    total_albums_scanned = 0
    
    # Start with the base search URL
    search_url = 'https://api.spotify.com/v1/search'
    auth_header = {"Authorization": f"Bearer {token}"}
    
    page_count = 0
    max_pages = 20 # Limit to 20 pages (1000 albums)
    
    # --- ** NEW: Random Offset Search ** ---
    # This ensures you get a new set of results each time you scan
    
    # 1. First, do a dummy search to find the total number of results
    total_results = 1000 # Default to 1000
    try:
        dummy_params = {'q': 'label:"Records DK"', 'type': 'album', 'limit': 1}
        dummy_response = requests.get(search_url, headers=auth_header, params=dummy_params)
        dummy_response.raise_for_status()
        total_results = dummy_response.json().get('albums', {}).get('total', 1000)
        # Spotify's max offset is 1000 (or 2000 results). We will cap it at 950 to be safe.
        app.logger.info(f"Total results for query: {total_results}")
    except Exception as e:
        app.logger.error(f"Error getting total results: {e}")
        
    # 2. Pick a random starting point (offset)
    # Max offset is 950 (page 20 * 50 albums/page = 1000 results limit)
    max_possible_offset = min(total_results, 950) 
    random_offset = random.randint(0, max_possible_offset // 50) * 50
    app.logger.info(f"Starting scan at random offset: {random_offset}")

    # Set initial parameters for the first request
    params = {
        'q': 'label:"Records DK"',  # This is still the best query
        'type': 'album',
        'limit': 50,       
        'offset': random_offset
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
                    
                    # --- ** NEW: Stricter Filter (Fixes "Bach Problem") ** ---
                    # We ONLY look for "records dk". We REMOVED "or 'distrokid'"
                    if copyright.get('type') == 'P' and 'records dk' in copyright_text:
                        for artist in album.get('artists', []):
                            artist_id = artist.get('id')
                            artist_name = artist.get('name')
                            artist_url = artist.get('external_urls', {}).get('spotify')
                            if artist_id and artist_name and artist_id not in artists_found:
                                # Store by ID to prevent duplicates
                                artists_found[artist_id] = {
                                    "name": artist_name,
                                    "url": artist_url
                                }
                        break # Found a match, move to the next album
            
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

    # --- ** NEW: Step 4 - Get Popularity and Followers ** ---
    if not artists_found:
        app.logger.info("Scan complete. Found 0 artists.")
        return jsonify({"artists": []})

    app.logger.info(f"Scan complete. Found {len(artists_found)} artists. Now fetching details...")
    artist_ids_list = list(artists_found.keys())
    detailed_artists = get_artist_details(artist_ids_list, token)

    final_artist_list = []
    for artist_data in detailed_artists:
        if not artist_data: continue
        artist_id = artist_data.get('id')
        base_info = artists_found.get(artist_id)
        if base_info:
            final_artist_list.append({
                "name": base_info['name'],
                "url": base_info['url'],
                "followers": artist_data.get('followers', {}).get('total', 0),
                "popularity": artist_data.get('popularity', 0)
            })

    app.logger.info("Successfully fetched all artist details.")
    # Sort by name by default
    final_artist_list_sorted = sorted(final_artist_list, key=lambda k: k['name'])
    
    return jsonify({"artists": final_artist_list_sorted})

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