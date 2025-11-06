import os
import base64
import time
import random
import string
import re
from flask import Flask, jsonify, make_response, request
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

# --- ** Stricter Regex filter for P-Line ** ---
# This looks for one or more digits (\d+), followed by a space (\s+),
# followed by "records dk". This is the strict pattern we need.
P_LINE_REGEX = re.compile(r"\d+\s+records\s+dk", re.IGNORECASE)

# --- ** Follower Threshold ** ---
# We will filter out any artist with more than this many followers.
MAX_FOLLOWERS = 100000


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

# --- ** NEW: Helper Function to Check Artist's Most Recent Release ** ---
def check_artist_most_recent_release(artist_id, token):
    """
    Gets the artist's most recent album/single and checks its P-line.
    Returns True if it matches, False otherwise.
    """
    try:
        # 1. Get the artist's most recent album or single
        url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
        params = {
            'include_groups': 'album,single',
            'limit': 1,
            'country': 'US' # Use US market as a standard
        }
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        if not data.get('items'):
            app.logger.warning(f"Artist {artist_id} has no recent items to check.")
            return False # Artist has no albums
            
        most_recent_album_id = data['items'][0]['id']

        # 2. Get the *full details* of that one album
        full_album_details = get_full_album_details([most_recent_album_id], token)
        if not full_album_details:
            app.logger.warning(f"Could not get details for recent album {most_recent_album_id}")
            return False

        # 3. Check its P-line
        for copyright in full_album_details[0].get('copyrights', []):
            copyright_text = copyright.get('text', '')
            if copyright.get('type') == 'P' and P_LINE_REGEX.search(copyright_text):
                app.logger.info(f"CONFIRMED: Artist {artist_id} most recent release has DK P-line.")
                return True # Found a match

        app.logger.info(f"REJECTED: Artist {artist_id} most recent release does NOT have DK P-line.")
        return False # No P-line match on most recent release
        
    except Exception as e:
        app.logger.error(f"Error checking recent release for artist {artist_id}: {e}")
        return False


# --- ** NEW API Route: /api/start_scan ** ---
@app.route('/api/start_scan')
def start_scan():
    """
    STEP 1: Performs multiple searches and returns a combined list of album IDs.
    This is fast and will not time out.
    """
    app.logger.info("Received scan request at /api/start_scan")
    token = get_spotify_token()
    
    if not token:
        app.logger.warning("Token request failed. Sending auth error to client.")
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    app.logger.info("Authentication successful. Starting combined album ID search...")
    all_album_ids = set() # Use a set to avoid duplicates
    auth_header = {"Authorization": f"Bearer {token}"}

    # --- ** SEARCH 1: "Records DK" (Targeted) ** ---
    try:
        search_url = 'https://api.spotify.com/v1/search'
        search_query = 'label:"Records DK"'
        
        # Get total results to find a random offset
        total_results = 1000
        try:
            dummy_params = {'q': search_query, 'type': 'album', 'limit': 1}
            dummy_response = requests.get(search_url, headers=auth_header, params=dummy_params)
            dummy_response.raise_for_status()
            total_results = dummy_response.json().get('albums', {}).get('total', 1000)
            app.logger.info(f"Total results for query '{search_query}': {total_results}")
        except Exception as e:
            app.logger.error(f"Error getting total results: {e}")
            
        max_possible_offset = min(total_results, 950) 
        random_offset = 0
        if max_possible_offset > 50:
             random_offset = random.randint(0, max_possible_offset // 50) * 50
        app.logger.info(f"Starting 'Records DK' scan at random offset: {random_offset}")

        # Scan 10 pages (500 albums) from this search
        next_url = search_url
        params = {'q': search_query, 'type': 'album', 'limit': 50, 'offset': random_offset}
        
        for page in range(10): # 10 pages * 50 albums/page = 500 albums
            if not next_url:
                break
            
            app.logger.debug(f"Scanning 'Records DK' page {page + 1}...")
            if page == 0:
                response = requests.get(search_url, headers=auth_header, params=params)
            else:
                response = requests.get(next_url, headers=auth_header)
                
            response.raise_for_status()
            data = response.json()
            
            for album in data.get('albums', {}).get('items', []):
                if album and album.get('id'):
                    all_album_ids.add(album['id'])
            
            next_url = data.get('albums', {}).get('next')
            time.sleep(0.05) # Be nice to API

    except Exception as e:
        app.logger.error(f"ERROR during 'Records DK' search: {e}")

    app.logger.info(f"Found {len(all_album_ids)} unique IDs from 'Records DK' search.")

    # --- ** SEARCH 2: "New Releases" (Diverse) ** ---
    try:
        browse_url = 'https://api.spotify.com/v1/browse/new-releases'
        next_url = browse_url
        params = {'limit': 50}
        
        for page in range(10): # 10 pages * 50 albums/page = 500 albums
            if not next_url:
                break
                
            app.logger.debug(f"Scanning 'New Releases' page {page + 1}...")
            if page == 0:
                response = requests.get(browse_url, headers=auth_header, params=params)
            else:
                response = requests.get(next_url, headers=auth_header)
            
            response.raise_for_status()
            data = response.json()

            for album in data.get('albums', {}).get('items', []):
                if album and album.get('id'):
                    all_album_ids.add(album['id'])
            
            next_url = data.get('albums', {}).get('next')
            time.sleep(0.05) # Be nice to API

    except Exception as e:
        app.logger.error(f"ERROR during 'New Releases' search: {e}")

    app.logger.info(f"Initial search complete. Found {len(all_album_ids)} total unique album IDs to process.")
    return jsonify({"album_ids": list(all_album_ids)})


# --- ** NEW API Route: /api/get_details ** ---
@app.route('/api/get_details', methods=['POST'])
def get_details():
    """
    STEP 2: Receives a list of album IDs from the frontend,
    filters them, and returns any artists found.
    """
    app.logger.info("Received request at /api/get_details")
    data = request.get_json()
    album_ids = data.get('album_ids')
    # ** NEW: Get the list of artists we've already found **
    artists_already_found = data.get('artists_already_found', [])

    if not album_ids:
        app.logger.error("No album IDs provided to /api/get_details")
        return jsonify({"error": "No album IDs provided"}), 400

    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    artists_to_fetch_details_for = {} # {id: {name, url}}
    
    # 1. GET FULL ALBUM DETAILS (with Copyrights)
    app.logger.debug(f"Getting full details for {len(album_ids)} albums...")
    full_albums = get_full_album_details(album_ids, token)

    # 2. FILTER THE FULL ALBUM DETAILS
    for album in full_albums:
        if not album: continue
        for copyright in album.get('copyrights', []):
            copyright_text = copyright.get('text', '') # No .lower() needed for regex
            
            # --- ** FINAL, STRICT REGEX FILTER ** ---
            if copyright.get('type') == 'P' and P_LINE_REGEX.search(copyright_text):
                for artist in album.get('artists', []):
                    artist_id = artist.get('id')
                    artist_name = artist.get('name')
                    
                    # ** NEW: Duplicate check **
                    if artist_id and artist_name and artist_id not in artists_already_found:
                        
                        # ** NEW: Check artist's most recent release **
                        if check_artist_most_recent_release(artist_id, token):
                            artists_to_fetch_details_for[artist_id] = {
                                "name": artist_name,
                                "url": artist.get('external_urls', {}).get('spotify')
                            }
                break # Found a match, move to the next album
    
    if not artists_to_fetch_details_for:
        app.logger.info("Chunk processed. Found 0 new artists.")
        return jsonify({"artists": []}) # Return empty list, not an error

    # 3. GET POPULARITY AND FOLLOWERS
    app.logger.info(f"Chunk processed. Found {len(artists_to_fetch_details_for)} new artists. Now fetching details...")
    artist_ids_list = list(artists_to_fetch_details_for.keys())
    detailed_artists = get_artist_details(artist_ids_list, token)

    final_artist_list = []
    for artist_data in detailed_artists:
        if not artist_data: continue
        artist_id = artist_data.get('id')
        base_info = artists_to_fetch_details_for.get(artist_id)
        if base_info:
            artist_followers = artist_data.get('followers', {}).get('total', 0)
            
            # 4. FILTER BY FOLLOWER CAP
            if artist_followers < MAX_FOLLOWERS:
                final_artist_list.append({
                    "name": base_info['name'],
                    "url": base_info['url'],
                    "followers": artist_followers,
                    "popularity": artist_data.get('popularity', 0),
                    "id": artist_id # ** NEW: Send ID to frontend for duplicate checking **
                })
            else:
                 app.logger.info(f"Filtering out artist: {base_info['name']} (Followers: {artist_followers})")

    app.logger.info(f"Returning {len(final_artist_list)} artists to frontend.")
    return jsonify({"artists": final_artist_list})


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