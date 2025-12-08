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
CORS(app)
logging.basicConfig(level=logging.DEBUG)

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
MAX_FOLLOWERS = 100000

# Regex: Matches "Records DK" or "DK <number>"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

# --- Global Token Cache ---
token_info = {
    'access_token': None,
    'expires_at': 0
}

# --- Session for Connection Pooling ---
# Improves speed by keeping the TCP connection to Spotify open
spotify_session = requests.Session()

def get_spotify_token():
    global token_info
    now = time.time()
    
    # Return cached token if still valid (with 60s buffer)
    if token_info['access_token'] and token_info['expires_at'] > now + 60:
        return token_info['access_token']

    if not CLIENT_ID or not CLIENT_SECRET:
        app.logger.error("CRITICAL: Missing credentials.")
        return None

    auth_url = 'https://accounts.spotify.com/api/token'
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_base64 = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
    
    headers = {
        "Authorization": f"Basic {auth_base64}", 
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()
        json_data = response.json()
        
        token_info['access_token'] = json_data.get('access_token')
        # Expires in usually 3600 seconds
        token_info['expires_at'] = now + json_data.get('expires_in', 3600)
        
        app.logger.info("Generated new Spotify Access Token")
        return token_info['access_token']
    except Exception as e:
        app.logger.error(f"Auth Error: {e}")
        return None

def make_spotify_request(url, params=None):
    token = get_spotify_token()
    if not token: return None
    
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = spotify_session.get(url, headers=headers, params=params)
        
        # Handle Rate Limiting
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 5))
            app.logger.warning(f"Rate limited. Sleeping {retry_after}s")
            time.sleep(retry_after)
            return make_spotify_request(url, params) # Retry
            
        response.raise_for_status()
        return response.json()
    except Exception as e:
        app.logger.error(f"Request Error ({url}): {e}")
        return None

def check_copyright_string(album):
    """Checks if any C or P line matches the target regex."""
    for copyright in album.get('copyrights', []):
        # FIX: Check both 'P' and 'C' types
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '')
            if P_LINE_REGEX.search(text):
                return True
    return False

def verify_artist_latest_release(artist_id):
    """
    Fetches the artist's albums, sorts by date, and checks if the 
    absolute newest release matches the target label.
    """
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    # Fetch singles and albums to be sure
    params = {'include_groups': 'album,single', 'limit': 10, 'market': 'US'}
    
    data = make_spotify_request(url, params)
    if not data: return False
    
    items = data.get('items', [])
    if not items: return False
    
    # Sort by release_date descending (newest first)
    # Spotify dates can be 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    
    # Get the ID of the absolute newest release
    latest_album_id = items[0].get('id')
    
    # We need to fetch full details for this specific album to see the copyright
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_album_id}"
    full_album = make_spotify_request(album_details_url)
    
    if full_album and check_copyright_string(full_album):
        return True
        
    return False

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    # 1. Random Search for Recent Albums
    current_year = datetime.datetime.now().year
    char1 = random.choice(string.ascii_lowercase)
    char2 = random.choice(string.ascii_lowercase)
    query = f"{char1}{char2}* year:{current_year}"
    
    search_data = make_spotify_request(
        'https://api.spotify.com/v1/search',
        params={'q': query, 'type': 'album', 'limit': 50, 'offset': 0, 'market': 'US'}
    )
    
    if not search_data: return jsonify({"artists": []})
    
    # Collect Album IDs
    album_ids = [item['id'] for item in search_data.get('albums', {}).get('items', []) if item]
    if not album_ids: return jsonify({"artists": []})

    # 2. Batch Fetch Album Details (to see Copyrights)
    # We split into chunks of 20
    candidate_artists = {}
    
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details_data = make_spotify_request(
            'https://api.spotify.com/v1/albums',
            params={'ids': ','.join(chunk)}
        )
        
        if not details_data: continue
        
        for album in details_data.get('albums', []):
            if not album: continue
            
            # Initial Check: Does THIS random album match?
            if check_copyright_string(album):
                for artist in album.get('artists', []):
                    aid = artist.get('id')
                    if aid and aid not in artists_already_found:
                        candidate_artists[aid] = {
                            'name': artist.get('name'),
                            'url': artist.get('external_urls', {}).get('spotify')
                        }

    if not candidate_artists:
        return jsonify({"artists": []})

    # 3. Validation Phase
    # We found candidates, but we must verify they satisfy the "Most Recent" rule
    # and the "Follower Count" rule.
    
    final_artists = []
    
    # Fetch Artist Details (for follower count)
    artist_ids = list(candidate_artists.keys())
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        artists_data = make_spotify_request(
            'https://api.spotify.com/v1/artists', 
            params={'ids': ','.join(chunk)}
        )
        
        if not artists_data: continue

        for artist_obj in artists_data.get('artists', []):
            if not artist_obj: continue
            
            aid = artist_obj.get('id')
            followers = artist_obj.get('followers', {}).get('total', 0)
            
            if followers < MAX_FOLLOWERS:
                # HEAVY CHECK: Only do this if they pass the follower filter
                # Verify their *actual* latest release matches
                if verify_artist_latest_release(aid):
                    base_info = candidate_artists[aid]
                    final_artists.append({
                        "name": base_info['name'],
                        "url": base_info['url'],
                        "followers": followers,
                        "popularity": artist_obj.get('popularity', 0),
                        "id": aid
                    })

    return jsonify({"artists": final_artists})

@app.route('/')
def serve_frontend():
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            return make_response(f.read())
    except FileNotFoundError:
        return "Error: frontend not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
