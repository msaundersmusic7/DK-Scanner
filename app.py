import os
import base64
import time
import random
import string
import re
import datetime 
from flask import Flask, jsonify, make_response, request
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
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- ** Regex filter for P-Line ** ---
# Searches for:
# 1. "records dk" (e.g., "3110243 Records DK")
# 2. "dk [number]" (e.g., "DK 123456")
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

# --- ** Follower Threshold ** ---
MAX_FOLLOWERS = 100000

# --- Helper Function: Get Access Token ---
def get_spotify_token():
    app.logger.debug("Attempting to get Spotify token...")
    if not CLIENT_ID or not CLIENT_SECRET:
        app.logger.error("CRITICAL: Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET environment variables.")
        return None

    auth_url = 'https://accounts.spotify.com/api/token'
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_bytes = auth_string.encode('utf-8')
    auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')
    headers = {"Authorization": f"Basic {auth_base64}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()
        token = response.json().get('access_token')
        if not token:
            app.logger.error("Authentication succeeded but no token was returned.")
            return None
        app.logger.debug("Successfully retrieved Spotify token.")
        return token
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during authentication: {e}")
        return None

# --- Helper Function: Get Full Album Details ---
def get_full_album_details(album_ids, token):
    if not album_ids:
        return []
    albums_url = 'https://api.spotify.com/v1/albums'
    auth_header = {"Authorization": f"Bearer {token}"}
    full_album_list = []
    
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i + 20]
        params = {'ids': ','.join(chunk)}
        try:
            response = requests.get(albums_url, headers=auth_header, params=params)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited getting album details. Waiting {retry_after}s...")
                time.sleep(retry_after)
                response = requests.get(albums_url, headers=auth_header, params=params)
            response.raise_for_status()
            data = response.json()
            full_album_list.extend(data.get('albums', []))
        except Exception as e:
            app.logger.error(f"Unexpected error in get_full_album_details: {e}")
    return full_album_list

# --- Helper Function to Get Artist Details ** ---
def get_artist_details(artist_ids, token):
    if not artist_ids:
        return []
    artists_url = 'https://api.spotify.com/v1/artists'
    auth_header = {"Authorization": f"Bearer {token}"}
    artist_details_list = []
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i + 50]
        params = {'ids': ','.join(chunk)}
        try:
            response = requests.get(artists_url, headers=auth_header, params=params)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                app.logger.warning(f"Rate limited getting artist details. Waiting {retry_after}s...")
                time.sleep(retry_after)
                response = requests.get(artists_url, headers=auth_header, params=params)
            response.raise_for_status()
            data = response.json()
            artist_details_list.extend(data.get('artists', []))
        except Exception as e:
            app.logger.error(f"Unexpected error in get_artist_details: {e}")
    return artist_details_list

# --- ** DELETED FUNCTION ** ---
# The check_artist_most_recent_release function was here.
# It has been permanently removed as it was the cause of the 30-second page loads.


# --- ** UNIFIED API Route: /api/scan_one_page ** ---
@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    """
    Receives a page index from the frontend.
    1. Fetches ONE page (50 albums) of random, recent releases.
    2. Processes just those 50.
    3. Returns any *new* artists found.
    """
    data = request.get_json()
    page_index = data.get('page_index', 0)
    artists_already_found = data.get('artists_already_found', [])
    
    app.logger.info(f"--- Processing Page {page_index} ---")

    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    auth_header = {"Authorization": f"Bearer {token}"}
    album_ids_from_page = set()
    
    # Get current year for filtering
    current_year = datetime.datetime.now().year
    
    try:
        # --- Step 1: Get 50 Album Summaries ---
        # ** FAST, DIVERSE SEARCH POOL **
        # We search for a random letter + the current year.
        
        search_url = 'https://api.spotify.com/v1/search'
        random_char = random.choice(string.ascii_lowercase)
        query = f"{random_char}* year:{current_year}"
        
        params = {
            'q': query,
            'type': 'album',
            'limit': 50,
            'offset': page_index * 50, # We use the page index as the offset for *this query*
            'market': 'US'
        }
        
        app.logger.debug(f"Running random search query: {query}, offset: {page_index * 50}")
        response = requests.get(search_url, headers=auth_header, params=params)
        response.raise_for_status()
        data = response.json()
        albums_page = data.get('albums', {}).get('items', [])

        if not albums_page:
            app.logger.info(f"Page {page_index}: No albums found for query, returning empty.")
            return jsonify({"artists": []})

        for album in albums_page:
            if album and album.get('id'):
                album_ids_from_page.add(album['id'])
    
    except requests.exceptions.HTTPError as err:
        app.logger.error(f"HTTP error getting album summaries: {err}")
        return jsonify({"artists": []}) 
    except Exception as e:
        app.logger.error(f"Unexpected error getting album summaries: {e}")
        return jsonify({"artists": []})

    if not album_ids_from_page:
        return jsonify({"artists": []})

    # --- Step 2: Get Full Details for these 50 Albums ---
    full_albums = get_full_album_details(list(album_ids_from_page), token)
    if not full_albums:
        return jsonify({"artists": []})

    # --- Step 3: Filter Albums for P-Line ---
    artists_to_fetch_details_for = {}
    for album in full_albums:
        if not album: continue
        for copyright in album.get('copyrights', []):
            copyright_text = copyright.get('text', '')
            
            # ** We are now scanning the P-LINE (copyright) **
            if copyright.get('type') == 'P' and P_LINE_REGEX.search(copyright_text):
                for artist in album.get('artists', []):
                    artist_id = artist.get('id')
                    artist_name = artist.get('name')
                    
                    if artist_id and artist_name and artist_id not in artists_already_found:
                        
                        # ** The slow validation check was removed **
                        artists_to_fetch_details_for[artist_id] = {
                            "name": artist_name,
                            "url": artist.get('external_urls', {}).get('spotify')
                        }
                
                break # Found match, move to next album

    if not artists_to_fetch_details_for:
        app.logger.info(f"Page {page_index}: Found P-lines, but no *new* artists.")
        return jsonify({"artists": []})

    # --- Step 4: Get Artist Details & Filter by Followers ---
    artist_ids_list = list(artists_to_fetch_details_for.keys())
    detailed_artists = get_artist_details(artist_ids_list, token)
    final_artist_list = []
    
    for artist_data in detailed_artists:
        if not artist_data: continue
        artist_id = artist_data.get('id')
        base_info = artists_to_fetch_details_for.get(artist_id)
        if base_info:
            artist_followers = artist_data.get('followers', {}).get('total', 0)
            if artist_followers < MAX_FOLLOWERS:
                final_artist_list.append({
                    "name": base_info['name'],
                    "url": base_info['url'],
                    "followers": artist_followers,
                    "popularity": artist_data.get('popularity', 0),
                    "id": artist_id 
                })
            else:
                 app.logger.info(f"Filtering out artist: {base_info['name']} (Followers: {artist_followers})")

    app.logger.info(f"Page {page_index}: Found {len(final_artist_list)} new artists.")
    return jsonify({"artists": final_artist_list})


# --- Frontend Route: / ---
@app.route('/')
def serve_frontend():
    app.logger.info("Serving frontend HTML at /")
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return make_response(html_content)
    except FileNotFoundError:
        app.logger.error("CRITICAL: spotify_scanner.html not found in root directory.")
        return "Error: Could not find frontend HTML file.", 404

# --- Main entry point to run the app ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)