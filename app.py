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
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
MAX_FOLLOWERS = 50000      
MIN_POPULARITY = 0         # Set to 0 to catch brand new artists
REQUIRE_IMAGE = True       # Still require an image (shows basic effort)
REQUIRE_GENRE = False      # DISABLED: Many new indie artists don't have genres tagged yet
MIN_RELEASES = 1           # 1 release is enough
DAYS_WINDOW = 365          # EXPANDED: 1 year window. (Maximizes results)

# --- MINIMALIST BOT FILTER ---
# Only blocking the absolute worst offenders.
BLOCKED_KEYWORDS = ["white noise", "sleep", "meditation", "lullaby", "frequency"]

# Regex: Matches "Records DK", "DK <number>", "DistroKid"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+|distrokid)", re.IGNORECASE)

# --- Global Token Cache ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
spotify_session.mount('https://', adapter)

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
        response = requests.post(auth_url, headers=headers, data=data, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        token_info['access_token'] = json_data.get('access_token')
        token_info['expires_at'] = now + json_data.get('expires_in', 3600)
        return token_info['access_token']
    except Exception as e:
        app.logger.error(f"Auth Error: {e}")
        return None

def make_request_with_token(url, token, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = spotify_session.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 429:
            return None 
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def clean_artist_name(name):
    """Removes 'The ' and standardizes for comparison."""
    if not name: return ""
    clean = name.lower()
    if clean.startswith("the "):
        clean = clean[4:]
    return clean.strip()

def check_copyright_match(album, artist_name=None):
    """
    Returns True if:
    1. 'Records DK' / 'DistroKid' is in the text.
    2. The Artist's name (or close variation) is in the text.
    """
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            
            # 1. Regex Match (Records DK)
            if P_LINE_REGEX.search(text): 
                return True
            
            # 2. Name Match
            if artist_name:
                clean_name = clean_artist_name(artist_name)
                # Check if "Band Name" is in "© 2025 Band Name LLC"
                if clean_name in text:
                    return True
    return False

def is_real_artist_name(name):
    name_lower = name.lower()
    for word in BLOCKED_KEYWORDS:
        if word in name_lower: return False
    return True

def is_quality_candidate(artist_obj):
    # Only filtering empty profiles and bots.
    if REQUIRE_IMAGE and not artist_obj.get('images'): return False
    # REMOVED: Genre check (too strict for new artists)
    if artist_obj.get('popularity', 0) < MIN_POPULARITY: return False
    if not is_real_artist_name(artist_obj.get('name', '')): return False
    return True

def check_single_artist_activity(package):
    """
    Worker function. Verifies the LATEST release.
    """
    aid, name, url, pop, followers, token = package
    
    api_url = f"https://api.spotify.com/v1/artists/{aid}/albums"
    params = {'include_groups': 'album,single', 'limit': 10, 'market': 'US'}
    data = make_request_with_token(api_url, token, params)
    
    if not data or not data.get('items'): return None
    items = data.get('items', [])
    
    # Sort by date descending
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest_release = items[0]
    
    # Recency Check
    release_date_str = latest_release.get('release_date', '2000-01-01')
    try:
        if len(release_date_str) == 4: r_date = datetime.datetime.strptime(release_date_str, "%Y")
        elif len(release_date_str) == 7: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m")
        else: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m-%d")
        
        if (datetime.datetime.now() - r_date).days > DAYS_WINDOW:
            return None # Too old
    except ValueError:
        return None

    # Copyright Check on LATEST RELEASE
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_release['id']}"
    full_album = make_request_with_token(album_details_url, token)
    
    if full_album:
        if check_copyright_match(full_album, name):
            return {
                "name": name,
                "url": url,
                "followers": followers,
                "popularity": pop,
                "id": aid
            }
    return None

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    # --- DEEP SEARCH STRATEGY ---
    current_year = datetime.datetime.now().year
    
    # Use just ONE char to get a huge bucket, but use OFFSET to dig deep
    char = random.choice(string.ascii_lowercase)
    # Random offset between 0 and 900 (Spotify limit is usually 1000-2000)
    # This jumps past the famous artists to the indie section
    rand_offset = random.randint(0, 900)
    
    query = f"{char}* year:{current_year}"
    
    # Fetch 50 albums from deep in the list
    search_data = make_request_with_token(
        'https://api.spotify.com/v1/search', token,
        {'q': query, 'type': 'album', 'limit': 50, 'offset': rand_offset, 'market': 'US'}
    )
    
    if not search_data: return jsonify({"artists": []})
    
    raw_albums = search_data.get('albums', {}).get('items', [])
    if not raw_albums: return jsonify({"artists": []})
    
    # Extract Artist IDs to process
    # We blindly grab ALL artist IDs from these albums to check them
    candidate_map = {} 
    
    for album in raw_albums:
        if not album: continue
        for artist in album.get('artists', []):
            aid = artist.get('id')
            name = artist.get('name')
            if aid and name and aid not in artists_already_found:
                candidate_map[aid] = {
                    'name': name,
                    'url': artist.get('external_urls', {}).get('spotify')
                }

    if not candidate_map: return jsonify({"artists": []})

    # Batch Process Artist Details
    artist_ids = list(candidate_map.keys())
    verified_candidates = [] 
    
    # We chunk in 50s
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        adata = make_request_with_token('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
        if not adata: continue
        
        for a_obj in adata.get('artists', []):
            if not a_obj: continue
            
            # Apply Light Filters
            if is_quality_candidate(a_obj):
                aid = a_obj.get('id')
                if aid in candidate_map:
                     verified_candidates.append((
                        aid,
                        candidate_map[aid]['name'],
                        candidate_map[aid]['url'],
                        a_obj.get('popularity', 0),
                        a_obj.get('followers', {}).get('total', 0),
                        token
                    ))

    # Threaded Verification
    final_results = []
    if verified_candidates:
        # Increased workers to 15 to chew through the queue faster
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_artist = {executor.submit(check_single_artist_activity, pkg): pkg for pkg in verified_candidates}
            for future in as_completed(future_to_artist):
                result = future.result()
                if result:
                    final_results.append(result)

    return jsonify({"artists": final_results})

@app.route('/')
def serve_frontend():
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            return make_response(f.read())
    except FileNotFoundError:
        return "Error: frontend not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)