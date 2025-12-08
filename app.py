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

# BLACKLIST: If any of these appear in the copyright text, we SKIP the artist immediately.
MAJOR_LABELS = [
    "sony", "universal", "umg", "warner", "atlantic", "columbia", "rca", 
    "interscope", "capitol", "republic", "def jam", "elektra", 
    "island records", "arista", "epic", "bad boy", "cash money", 
    "roc nation", "aftermath", "shady", "young money", "concord", "bmg"
]

# --- Auth & Session ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
spotify_session.mount('https://', adapter)

# THROTTLE: 0.2s minimum between calls to stay safe
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

    # Official Auth URL
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
                    app.logger.error(f"LONG BAN ({retry_after}s). Aborting request.")
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
    if not artist_name: return False
    
    # 1. Clean the Artist Name
    # "The Band" -> "band"
    # "Artist Name" -> "artistname"
    artist_clean = artist_name.lower()
    if artist_clean.startswith("the "): artist_clean = artist_clean[4:]
    artist_clean = re.sub(r'[^a-z0-9]', '', artist_clean)

    # Regex for Explicit DistroKid
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for copyright in album.get('copyrights', []):
        text = copyright.get('text', '')
        if not text: continue
        
        text_lower = text.lower()
        
        # 2. BLACKLIST CHECK: If major label found, immediate fail.
        if any(major in text_lower for major in MAJOR_LABELS): 
            # app.logger.info(f"Rejected Major Label: {text}")
            return False

        # 3. PASS: "Records DK" found
        if dk_regex.search(text):
            return True
        
        # 4. STRICT CHECK: Exact Artist Name
        # We strip the year (e.g., "2024") and common symbols from the copyright text
        # "© 2025 Artist Name" -> "artistname"
        # "℗ 2024 Artist Name LLC" -> "artistnamellc"
        
        # Remove years (4 digits)
        text_no_year = re.sub(r'\d{4}', '', text_lower)
        # Remove special chars -> clean string
        text_clean = re.sub(r'[^a-z0-9]', '', text_no_year)
        
        # STRICT COMPARISON:
        # Instead of "is artist IN text", we check if they are ALMOST EQUAL.
        # This prevents "Sky" matching "Sky High Records".
        # We allow a small difference (like "llc" or "records") but the core must be the name.
        
        if artist_clean == text_clean:
            return True
            
        # Allow match if text is just "artistname" + "records" or "music"
        # e.g. Artist: "Cool" matches "Cool Music"
        if text_clean in [artist_clean + "records", artist_clean + "music", artist_clean + "entertainment", "records" + artist_clean]:
            return True
            
    return False

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
        
        # Search Strategy: Random Slice of 2024-2025 releases
        offset = random.randint(0, 950)
        query = "year:2024-2025" 
        
        app.logger.info(f"Scanning [Attempt {attempts}]: Offset {offset}...")

        search_data = make_request('https://api.spotify.com/v1/search', token, {'q': query, 'type': 'album', 'limit': 50, 'offset': offset, 'market': 'US'})
        if not search_data: continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: continue
        
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates = {} 
        
        # Batch Fetch Album Details (To check C/P lines)
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i:i+20]
            details = make_request('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
            if not details: continue
            
            for album in details.get('albums', []):
                if not album or not album.get('artists'): continue
                primary_artist = album['artists'][0]
                aid = primary_artist.get('id')
                
                if aid in artists_already_found: continue
                
                # STRICT FILTERING
                if check_copyright_match(album, primary_artist.get('name')):
                    candidates[aid] = {'name': primary_artist.get('name'), 'url': primary_artist.get('external_urls', {}).get('spotify')}

        if not candidates: continue

        # Batch Fetch Artist Details (To check Followers)
        artist_ids = list(candidates.keys())
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
                if aid in candidates:
                    app.logger.info(f"MATCH: {candidates[aid]['name']} ({followers})")
                    final_results.append({
                        "name": candidates[aid]['name'],
                        "url": candidates[aid]['url'],
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