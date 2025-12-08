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
MAX_FOLLOWERS = 300000 
REQUIRE_IMAGE = True

# BLACKLIST: Immediate disqualification if found in ANY copyright line
MAJOR_LABELS = [
    "sony", "universal", "umg", "warner", "atlantic", "columbia", "rca", 
    "interscope", "capitol", "republic", "def jam", "elektra", 
    "island records", "arista", "epic", "bad boy", "cash money", 
    "roc nation", "aftermath", "shady", "young money", "concord", "bmg",
    "kobalt", "ada", "300 entertainment", "empire", "utg", "create music group"
]

# --- Auth & Session ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
spotify_session.mount('https://', adapter)

# THROTTLE: Safety delay
MIN_REQUEST_INTERVAL = 0.2 
last_request_time = 0

def get_spotify_token():
    global token_info
    now = time.time()
    if token_info['access_token'] and token_info['expires_at'] > now + 60:
        return token_info['access_token']

    if not CLIENT_ID or not CLIENT_SECRET:
        app.logger.error("CRITICAL: Missing credentials.")
        return None

    # STANDARD API URL
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
        # Throttle
        now = time.time()
        elapsed = now - last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        
        try:
            last_request_time = time.time()
            response = spotify_session.get(url, headers=headers, params=params, timeout=10)
            
            # Rate Limit Handling
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 5)) + 1
                if retry_after > 60:
                    app.logger.error(f"LONG BAN ({retry_after}s). Aborting.")
                    return None 
                app.logger.warning(f"Rate limit hit. Sleeping {retry_after}s...")
                time.sleep(retry_after)
                continue
            
            if response.status_code == 200:
                return response.json()
            
            return None
            
        except Exception as e:
            app.logger.error(f"Connection error: {e}")
            time.sleep(1)
    
    return None

def check_copyright_match(album, artist_name):
    """
    STRICT MODE:
    1. Scan ALL lines for Major Labels -> Reject.
    2. Check for 'Records DK' or Exact Artist Name -> Accept.
    """
    if not artist_name: return False
    
    copyrights = [c.get('text', '').lower() for c in album.get('copyrights', []) if c.get('text')]
    if not copyrights: return False

    # STEP 1: The Purge (Major Labels)
    for text in copyrights:
        if any(major in text for major in MAJOR_LABELS):
            return False

    # STEP 2: The Match
    artist_clean = re.sub(r'[^a-z0-9]', '', artist_name.lower())
    if artist_clean.startswith("the"): artist_clean = artist_clean[3:] # Remove "the" strict
    
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for text in copyrights:
        # Match A: DistroKid
        if dk_regex.search(text):
            return True
            
        # Match B: Exact Artist Name
        # Remove years (2025) and symbols
        text_no_year = re.sub(r'\d{4}', '', text) 
        text_clean = re.sub(r'[^a-z0-9]', '', text_no_year)
        
        # Strict equality check
        if artist_clean == text_clean:
            return True
            
        # Allow standard suffixes if strictly attached
        if text_clean.startswith(artist_clean):
            suffix = text_clean[len(artist_clean):]
            if suffix in ["records", "music", "entertainment", "llc", "inc", "productions", "band", "group"]:
                return True
                
    return False

def get_latest_release_copyrights(artist_id, token, original_artist_name):
    """
    Fetches the artist's discography, sorts by date, 
    and checks copyrights of the absolute latest release.
    """
    # 1. Get Discography (Albums & Singles)
    # We fetch 20 items to be safe
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    data = make_request(url, token, {'include_groups': 'album,single', 'limit': 20, 'market': 'US'})
    
    if not data: return False
    
    items = data.get('items', [])
    if not items: return False
    
    # 2. Sort by Release Date (Newest First)
    # Date format can be '2025-01-01' or '2025'. String sort works reasonably well for ISO dates.
    sorted_items = sorted(items, key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest_album_summary = sorted_items[0]
    latest_id = latest_album_summary.get('id')
    
    if not latest_id: return False
    
    # 3. Get Full Details of Latest Release (to see Copyrights)
    details_url = f"https://api.spotify.com/v1/albums/{latest_id}"
    album_details = make_request(details_url, token)
    
    if not album_details: return False
    
    # 4. Check Copyrights on this latest release
    # app.logger.info(f"   -> Auditing Latest Release: {album_details.get('name')} ({album_details.get('release_date')})")
    return check_copyright_match(album_details, original_artist_name)

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    final_results = []
    attempts = 0
    max_attempts = 15 
    
    while len(final_results) < 5 and attempts < max_attempts:
        attempts += 1
        
        offset = random.randint(0, 950)
        query = "year:2024-2025" 
        
        app.logger.info(f"Scanning [Attempt {attempts}]: Offset {offset}...")

        # 1. SEARCH (Broad Sweep)
        search_data = make_request(
            'https://api.spotify.com/v1/search', 
            token, 
            {'q': query, 'type': 'album', 'limit': 50, 'offset': offset, 'market': 'US'}
        )
        if not search_data: continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: continue
        
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates_step1 = {} 
        
        # 2. INITIAL FILTER (Check found album)
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
                
                # STEP 1: Fast Check
                if check_copyright_match(album, name):
                    candidates_step1[aid] = name

        if not candidates_step1: continue

        # 3. DEEP VERIFICATION (Check Latest Release)
        # Only verify the survivors of Step 1
        verified_candidates = {}
        
        for aid, name in candidates_step1.items():
            # Throttle slightly
            time.sleep(0.1)
            
            # CALL THE NEW FUNCTION
            if get_latest_release_copyrights(aid, token, name):
                verified_candidates[aid] = name
            else:
                # app.logger.info(f"   -> {name} FAILED Latest Release Check.")
                pass

        if not verified_candidates: continue

        # 4. FINAL DETAILS (Followers)
        artist_ids = list(verified_candidates.keys())
        for i in range(0, len(artist_ids), 50):
            chunk = artist_ids[i:i+50]
            adata = make_request('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
            if not adata: continue
            
            for a_obj in adata.get('artists', []):
                if not a_obj: continue
                followers = a_obj.get('followers', {}).get('total', 0)
                
                if followers > MAX_FOLLOWERS: continue
                if REQUIRE_IMAGE and not a_obj.get('images'): continue
                
                aid = a_obj.get('id')
                if aid in verified_candidates:
                    app.logger.info(f"MATCH: {verified_candidates[aid]} ({followers} followers)")
                    final_results.append({
                        "name": verified_candidates[aid],
                        "url": a_obj.get('external_urls', {}).get('spotify'),
                        "followers": followers,
                        "popularity": a_obj.get('popularity', 0),
                        "id": aid
                    })
                    artists_already_found.add(aid)

            if len(final_results) >= 10: break

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