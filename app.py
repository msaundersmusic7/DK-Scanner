import os
import base64
import time
import random
import re
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS
import requests
import logging

# --- Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
MAX_FOLLOWERS = 200000 
REQUIRE_IMAGE = True

# BLACKLIST: Immediate rejection if found in copyright
MAJOR_LABELS = [
    "sony", "universal", "umg", "warner", "atlantic", "columbia", "rca", 
    "interscope", "capitol", "republic", "def jam", "elektra", 
    "island records", "arista", "epic", "bad boy", "cash money", 
    "roc nation", "aftermath", "shady", "young money", "concord", "bmg",
    "kobalt", "ada", "300 entertainment", "empire", "utg", "create music group",
    "distributor", "distribution", "records dk2" 
]

# ALLOWED SUFFIXES: Words that independent artists often add to their name in copyrights
# e.g. "Artist Name LLC" or "Artist Name Productions"
ALLOWED_SUFFIXES = [
    "records", "music", "entertainment", "llc", "inc", "productions", 
    "band", "group", "collective", "ent", "official", "publishing", "ltd"
]

# --- Auth & Session ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
spotify_session.mount('https://', adapter)

MIN_REQUEST_INTERVAL = 0.15 # Slightly faster throttle
last_request_time = 0

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

def make_request(url, token, params=None):
    global last_request_time
    headers = {"Authorization": f"Bearer {token}"}
    
    for attempt in range(3):
        now = time.time()
        elapsed = now - last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        
        try:
            last_request_time = time.time()
            response = spotify_session.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 5)) + 1
                if retry_after > 60:
                    return None 
                time.sleep(retry_after)
                continue
            
            if response.status_code == 200:
                return response.json()
            return None
        except Exception:
            time.sleep(1)
    return None

def normalize_text(text):
    if not text: return ""
    clean = text.lower().strip()
    if clean.startswith("the "): clean = clean[4:]
    return re.sub(r'[^a-z0-9]', '', clean)

def check_copyright_match(album, artist_name, debug=False):
    """
    STRICT MODE VALIDATION
    debug=True will print why it failed.
    """
    if not artist_name: return False
    
    copyrights = [c.get('text', '').lower() for c in album.get('copyrights', []) if c.get('text')]
    if not copyrights: 
        if debug: print(f"   [REJECT] {artist_name}: No copyright data found.")
        return False

    # 1. THE BLACKLIST
    for text in copyrights:
        for major in MAJOR_LABELS:
            if major in text:
                if debug: print(f"   [REJECT] {artist_name}: Major label '{major}' found in '{text}'")
                return False

    # 2. THE MATCH
    artist_clean = normalize_text(artist_name)
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for text in copyrights:
        # Match A: DistroKid
        if dk_regex.search(text):
            if debug: print(f"   [MATCH] {artist_name}: 'Records DK' found in '{text}'")
            return True
            
        # Match B: Exact Artist Name (with smart suffix tolerance)
        text_no_year = re.sub(r'\d{4}', '', text) 
        text_clean = normalize_text(text_no_year)
        
        # Exact match
        if artist_clean == text_clean:
            if debug: print(f"   [MATCH] {artist_name}: Exact match '{text}'")
            return True
            
        # Suffix match (e.g. "Artist LLC")
        if text_clean.startswith(artist_clean):
            suffix = text_clean[len(artist_clean):]
            if suffix in ALLOWED_SUFFIXES:
                if debug: print(f"   [MATCH] {artist_name}: Suffix match '{text}'")
                return True
                
    if debug: print(f"   [REJECT] {artist_name}: Copyright '{copyrights[0]}' != '{artist_clean}'")
    return False

def get_latest_release_check(artist_id, token, original_artist_name):
    # 1. Fetch Latest Release
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    # limit=5 is enough to find the latest
    data = make_request(url, token, {'include_groups': 'album,single', 'limit': 5, 'market': 'US'})
    
    if not data or not data.get('items'): return False, None
    
    # Sort by date
    items = data.get('items')
    sorted_items = sorted([i for i in items if i.get('release_date')], key=lambda x: x.get('release_date'), reverse=True)
    if not sorted_items: return False, None
    
    latest_release = sorted_items[0]
    
    # 2. Check details
    details_url = f"https://api.spotify.com/v1/albums/{latest_release['id']}"
    album_details = make_request(details_url, token)
    if not album_details: return False, None
    
    # 3. Verify
    # print(f"   -> Verifying Latest: {latest_release['name']} ({latest_release['release_date']})")
    match = check_copyright_match(album_details, original_artist_name, debug=False) # Keep debug False here to reduce noise
    
    if match:
        return True, latest_release.get('external_urls', {}).get('spotify')
    return False, None

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    final_results = []
    attempts = 0
    
    # We will search aggressively until we find artists
    max_search_attempts = 50 
    
    while len(final_results) < 5 and attempts < max_search_attempts:
        attempts += 1
        
        # Focus purely on 2025/2024 for "Most Recent" relevance
        offset = random.randint(0, 950)
        query = "year:2024-2025"
        
        app.logger.info(f"--- SEARCHING PAGE {attempts} (Offset {offset}) ---")

        search_data = make_request(
            'https://api.spotify.com/v1/search', 
            token, 
            {'q': query, 'type': 'album', 'limit': 50, 'offset': offset, 'market': 'US'}
        )
        if not search_data: continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: 
            app.logger.info("   -> Empty page.")
            continue
        
        # Batch Fetch IDs
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        potential_candidates = {}
        
        # Check initial batch
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i:i+20]
            details = make_request('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
            if not details: continue
            
            for album in details.get('albums', []):
                if not album or not album.get('artists'): continue
                
                primary_artist = album['artists'][0]
                aid = primary_artist.get('id')
                name = primary_artist.get('name')
                
                if aid in artists_already_found: continue
                if aid in potential_candidates: continue 
                
                # DEBUG: Print failures for the first few attempts so user sees logic
                # Only print first 2 items per batch to avoid flooding
                debug_flag = (len(potential_candidates) < 1) 
                
                if check_copyright_match(album, name, debug=debug_flag):
                    potential_candidates[aid] = name

        if not potential_candidates: 
            app.logger.info("   -> No matches on this page.")
            continue
        
        app.logger.info(f"   -> Found {len(potential_candidates)} potential candidates. Verifying latest releases...")

        # Verify Latest Releases
        for aid, name in potential_candidates.items():
            if len(final_results) >= 5: break
            
            is_valid, spotify_url = get_latest_release_check(aid, token, name)
            
            if is_valid:
                # Get Artist Profile
                artist_data = make_request(f"https://api.spotify.com/v1/artists/{aid}", token)
                if not artist_data: continue
                
                followers = artist_data.get('followers', {}).get('total', 0)
                
                if followers <= MAX_FOLLOWERS:
                    if REQUIRE_IMAGE and not artist_data.get('images'): continue
                    
                    app.logger.info(f"SUCCESS: {name} ({followers} flwrs) - {spotify_url}")
                    final_results.append({
                        "name": name,
                        "url": artist_data.get('external_urls', {}).get('spotify'),
                        "followers": followers,
                        "popularity": artist_data.get('popularity', 0),
                        "id": aid
                    })
                    artists_already_found.add(aid)
            else:
                 # If they failed the "Latest Release" check, it means they might have signed recently
                 # app.logger.info(f"   -> {name} FAILED Latest Release Check.")
                 pass

    app.logger.info(f"Scan complete. Found {len(final_results)} artists.")
    return jsonify({"artists": final_results})

@app.route('/')
def serve_frontend():
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            return make_response(f.read())
    except FileNotFoundError:
        return "Error: spotify_scanner.html not found.", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)