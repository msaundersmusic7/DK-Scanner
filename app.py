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
from bs4 import BeautifulSoup # <-- For scraping Google

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


# --- ** NEW: Helper Function to Scrape Google ** ---
def scrape_google_for_spotify_links():
    """
    Scrapes Google for 'site:open.spotify.com/album "Records DK"'
    and returns a set of unique Spotify Album IDs.
    """
    app.logger.info("Scraping Google for Spotify links...")
    google_url = "https://www.google.com/search"
    # This query finds albums on Spotify that explicitly mention "Records DK"
    google_query = 'site:open.spotify.com/album "Records DK"'
    
    # We must use a User-Agent, or Google will block us with a 403 error
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.88 Safari/537.36'
    }
    
    album_ids = set()
    
    # ** NEW: Scan 3 pages of results, starting at a random page **
    # Pick a random starting page (0, 10, 20, ..., 50)
    start_index = random.choice(range(0, 51, 10))
    
    for page in range(3): # Scan 3 pages
        start = start_index + (page * 10)
        params = {
            'q': google_query,
            'num': 10, # 10 results per page
            'start': start
        }
        
        app.logger.info(f"Scraping Google page {page + 1} (start index {start})...")
        
        try:
            response = requests.get(google_url, params=params, headers=headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # This regex finds Spotify album links and extracts the ID
            album_link_pattern = re.compile(r"https://open\.spotify\.com/album/([a-zA-Z0-9]+)")
            
            for a in soup.find_all('a'):
                href = a.get('href')
                if href:
                    match = album_link_pattern.search(href)
                    if match:
                        album_id = match.group(1)
                        album_ids.add(album_id)
            
            time.sleep(0.5) # Be nice to Google
            
        except requests.exceptions.HTTPError as err:
            app.logger.error(f"HTTP error while scraping Google: {err}")
            # Don't stop, just try the next page
        except Exception as e:
            app.logger.error(f"Unexpected error while scraping Google: {e}")
            
    app.logger.info(f"Google scrape found {len(album_ids)} unique album IDs.")
    return list(album_ids)


# --- Main API Route: /api/scan ---
@app.route('/api/scan')
def scan_for_artists():
    """
    Main API endpoint to scan Spotify and return a list of artists.
    """
    app.logger.info("Received scan request at /api/scan")
    
    # --- ** NEW, FASTER LOGIC ** ---
    
    # 1. Scrape Google FIRST to get a high-quality list of IDs
    album_ids = scrape_google_for_spotify_links()
    
    if not album_ids:
        app.logger.info("Google scrape found 0 IDs. Returning 0 artists.")
        return jsonify({"artists": []})
        
    # 2. Get Spotify token to enrich this list
    token = get_spotify_token()
    if not token:
        app.logger.warning("Token request failed. Sending auth error to client.")
        return jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500

    app.logger.info(f"Authentication successful. Enriching {len(album_ids)} album IDs from Spotify...")
    artists_found = {} # Use a dictionary to store {id: {name, url}}
    
    # 3. Get Full Album Details (with Copyrights)
    app.logger.debug(f"Getting full details for {len(album_ids)} albums...")
    full_albums = get_full_album_details(album_ids, token)

    # 4. Filter the Full Album Details
    for album in full_albums:
        if not album: continue
        for copyright in album.get('copyrights', []):
            copyright_text = copyright.get('text', '') # No .lower() needed for regex
            
            # --- ** FINAL, STRICT REGEX FILTER ** ---
            # We ONLY look for the pattern "[Numbers] Records DK"
            if copyright.get('type') == 'P' and P_LINE_REGEX.search(copyright_text):
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
    
    app.logger.info(f"Scan complete. Found {len(artists_found)} unique artists so far.")

    # 5. Get Popularity and Followers (Back by popular demand!)
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
    
    # 6. Filter out "Bach Problem"
    filtered_artist_list = []
    for artist in final_artist_list:
        if artist['followers'] < MAX_FOLLOWERS:
            filtered_artist_list.append(artist)
        else:
            app.logger.info(f"Filtering out artist: {artist['name']} (Followers: {artist['followers']})")
            
    app.logger.info(f"Returning {len(filtered_artist_list)} artists after filtering.")
    
    # Sort by name by default
    final_artist_list_sorted = sorted(filtered_artist_list, key=lambda k: k['name'])
    
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