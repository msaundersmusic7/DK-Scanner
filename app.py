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

# --- REFINEMENT SETTINGS ---
MAX_FOLLOWERS = 50000      # Lowered slightly to target more reachable indie artists
MIN_POPULARITY = 5         # Bumped to 5 to avoid "ghost" accounts
REQUIRE_IMAGE = True       
REQUIRE_GENRE = True       

# NEW: Strict Activity Filters
MIN_RELEASES = 2           # Artist must have at least 2 items (Single/Album)
DAYS_WINDOW = 90           # Latest release must be within 90 days

# Regex: Matches "Records DK" or "DK <number>"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

# --- Global Token Cache ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()

def get_spotify_token():
    global token_info
    now = time.time()
    if token_info['access_token'] and token_info['expires_at'] > now + 60:
        return token_info['access_token']

    if not CLIENT_ID or not CLIENT_SECRET:
        app.logger.error("CRITICAL: Missing credentials.")
        return None

    auth_url = 'https://accounts.spotify.com/api/token'
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_base64 = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
    headers = {"Authorization": f"Basic {auth_base64}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()
        json_data = response.json()
        token_info['access_token'] = json_data.get('access_token')
        token_info['expires_at'] = now + json_data.get('expires_in', 3600)
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
        if response.status_code == 429:
            time.sleep(int(response.headers.get('Retry-After', 5)))
            return make_spotify_request(url, params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        app.logger.error(f"Request Error ({url}): {e}")
        return None

def check_copyright_match(album, artist_name=None):
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            if P_LINE_REGEX.search(text): return True
            if artist_name and artist_name.lower() in text: return True
    return False

def is_quality_candidate(artist_obj):
    # 1. Image Check
    if REQUIRE_IMAGE and not artist_obj.get('images'): return False
    # 2. Genre Check
    if REQUIRE_GENRE and not artist_obj.get('genres'): return False
    # 3. Popularity Check
    if artist_obj.get('popularity', 0) < MIN_POPULARITY: return False
    return True

def verify_artist_activity(artist_id, artist_name):
    """
    Checks:
    1. Total release count >= MIN_RELEASES
    2. Latest release is within DAYS_WINDOW
    3. Latest release matches copyright criteria
    """
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    params = {'include_groups': 'album,single', 'limit': 20, 'market': 'US'}
    
    data = make_spotify_request(url, params)
    if not data or not data.get('items'): return False
    
    items = data.get('items', [])
    
    # FILTER: Catalog Depth
    if len(items) < MIN_RELEASES:
        return False

    # Sort by release_date descending
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest_release = items[0]
    
    # FILTER: Recency
    release_date_str = latest_release.get('release_date', '2000-01-01')
    try:
        # Handle YYYY, YYYY-MM, or YYYY-MM-DD
        if len(release_date_str) == 4: release_date = datetime.datetime.strptime(release_date_str, "%Y")
        elif len(release_date_str) == 7: release_date = datetime.datetime.strptime(release_date_str, "%Y-%m")
        else: release_date = datetime.datetime.strptime(release_date_str, "%Y-%m-%d")
        
        days_diff = (datetime.datetime.now() - release_date).days
        if days_diff > DAYS_WINDOW:
            return False # Too old
    except ValueError:
        return False

    # FILTER: Copyright Match on LATEST release
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_release['id']}"
    full_album = make_spotify_request(album_details_url)
    
    if full_album and check_copyright_match(full_album, artist_name):
        return True
        
    return False

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    # 1. Random Search (Current Year)
    current_year = datetime.datetime.now().year
    char1 = random.choice(string.ascii_lowercase)
    char2 = random.choice(string.ascii_lowercase)
    query = f"{char1}{char2}* year:{current_year}"
    
    search_data = make_spotify_request(
        'https://api.spotify.com/v1/search',
        params={'q': query, 'type': 'album', 'limit': 50, 'offset': 0, 'market': 'US'}
    )
    
    if not search_data: return jsonify({"artists": []})
    album_ids = [item['id'] for item in search_data.get('albums', {}).get('items', []) if item]
    if not album_ids: return jsonify({"artists": []})

    # 2. Batch Fetch Album Details
    candidate_artists = {}
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details_data = make_spotify_request('https://api.spotify.com/v1/albums', params={'ids': ','.join(chunk)})
        if not details_data: continue
        
        for album in details_data.get('albums', []):
            if not album: continue
            has_dk_match = check_copyright_match(album, artist_name=None)
            
            for artist in album.get('artists', []):
                aid = artist.get('id')
                name = artist.get('name')
                if aid and name and aid not in artists_already_found:
                    if has_dk_match or check_copyright_match(album, artist_name=name):
                        candidate_artists[aid] = {'name': name, 'url': artist.get('external_urls', {}).get('spotify')}

    if not candidate_artists: return jsonify({"artists": []})

    # 3. Validation Phase
    final_artists = []
    artist_ids = list(candidate_artists.keys())
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        artists_data = make_spotify_request('https://api.spotify.com/v1/artists', params={'ids': ','.join(chunk)})
        if not artists_data: continue

        for artist_obj in artists_data.get('artists', []):
            if not artist_obj: continue
            aid = artist_obj.get('id')
            followers = artist_obj.get('followers', {}).get('total', 0)
            
            # Apply all filters
            if followers < MAX_FOLLOWERS and is_quality_candidate(artist_obj):
                name = candidate_artists[aid]['name']
                # Heavy check (Release counts, dates)
                if verify_artist_activity(aid, name):
                    final_artists.append({
                        "name": name,
                        "url": candidate_artists[aid]['url'],
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