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

# Explicitly block Major Labels to ensure "Artist Name" matches are actually indie
MAJOR_LABELS = [
    "sony", "universal", "warner", "atlantic", "columbia", "rca", 
    "interscope", "capitol", "republic", "def jam", "elektra", 
    "island records", "arista", "epic"
]

# --- Auth & Session ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
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

def make_request(url, token, params=None):
    """
    Robust request handler that sleeps automatically when rate limited.
    """
    headers = {"Authorization": f"Bearer {token}"}
    
    # Retry up to 4 times if rate limited
    for attempt in range(4):
        try:
            response = spotify_session.get(url, headers=headers, params=params, timeout=10)
            
            # CASE 1: Rate Limited (429)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 5)) + 1
                app.logger.warning(f"Rate limit hit. Sleeping {retry_after}s... (Attempt {attempt+1}/4)")
                time.sleep(retry_after)
                continue # Retry the loop
            
            # CASE 2: Success
            if response.status_code == 200:
                return response.json()
            
            # CASE 3: Other Error
            return None
            
        except Exception as e:
            app.logger.error(f"Connection error: {e}")
            time.sleep(1)
    
    return None

def check_copyright_match(album, artist_name):
    """
    Returns True if:
    1. 'Records DK' / 'DistroKid' is found.
    2. The Artist's Name appears in the copyright text (Self-Release).
    """
    if not artist_name: return False
    
    # Normalize Artist Name (remove "The", lowercase, remove special chars)
    # Example: "The Cool Band!" -> "coolband"
    artist_clean = artist_name.lower()
    if artist_clean.startswith("the "): artist_clean = artist_clean[4:]
    artist_clean = re.sub(r'[^a-z0-9]', '', artist_clean)
    
    # Regex for DistroKid / DK markers
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for copyright in album.get('copyrights', []):
        text = copyright.get('text', '')
        if not text: continue
        
        text_lower = text.lower()
        
        # 1. IMMEDIATE FAIL: Major Labels
        if any(major in text_lower for major in MAJOR_LABELS):
            return False

        # 2. SUCCESS: "Records DK" or "DistroKid"
        if dk_regex.search(text):
            return True
        
        # 3. SUCCESS: Artist Name Match (Self-Released)
        # Normalize copyright text: "© 2025 The Cool Band LLC" -> "2025thecoolbandllc"
        text_clean = re.sub(r'[^a-z0-9]', '', text_lower)
        
        # Check if "coolband" is inside "2025thecoolbandllc"
        # Minimum length 3 prevents matching short names like "X" or "Ra" falsely
        if len(artist_clean) >= 3 and artist_clean in text_clean:
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
    max_attempts = 15 # Will try 15 different random searches if needed
    
    while len(final_results) < 5 and attempts < max_attempts:
        attempts += 1
        
        # --- SIMPLE SEARCH STRATEGY ---
        # Search all 2024-2025 releases.
        # Use random offset (0-950) to jump to a random spot in the list.
        offset = random.randint(0, 950)
        query = "year:2024-2025" 
        
        app.logger.info(f"Scanning [Attempt {attempts}]: Offset {offset}...")

        # 1. SEARCH
        search_data = make_request(
            'https://api.spotify.com/v1/search', token,
            {
                'q': query, 
                'type': 'album', 
                'limit': 50, 
                'offset': offset, 
                'market': 'US' 
            }
        )
        
        if not search_data: 
            time.sleep(1) # Safety pause if search fails
            continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: continue
        
        # 2. GET COPYRIGHTS (Batch Request)
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates = {} 
        
        # Throttle: Small sleep before firing the next batch to be polite to API
        time.sleep(0.2)
        
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i:i+20]
            details = make_request('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
            if not details: continue
            
            for album in details.get('albums', []):
                if not album or not album.get('artists'): continue
                
                primary_artist = album['artists'][0]
                name = primary_artist.get('name')
                aid = primary_artist.get('id')

                if aid in artists_already_found: continue

                # CHECK: Does copyright match DK or the Artist's own name?
                if check_copyright_match(album, name):
                    candidates[aid] = {
                        'name': name,
                        'url': primary_artist.get('external_urls', {}).get('spotify')
                    }

        if not candidates: continue

        # 3. GET FOLLOWERS (Batch Request)
        artist_ids = list(candidates.keys())
        
        # Throttle again
        time.sleep(0.2)
        
        for i in range(0, len(artist_ids), 50):
            chunk = artist_ids[i:i+50]
            adata = make_request('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
            if not adata: continue
            
            for a_obj in adata.get('artists', []):
                if not a_obj: continue
                
                followers = a_obj.get('followers', {}).get('total', 0)
                
                # Filter: Skip if too famous or no image
                if followers > MAX_FOLLOWERS: continue
                if REQUIRE_IMAGE and not a_obj.get('images'): continue
                
                aid = a_obj.get('id')
                if aid in candidates:
                    app.logger.info(f"MATCH: {candidates[aid]['name']} ({followers} followers)")
                    final_results.append({
                        "name": candidates[aid]['name'],
                        "url": candidates[aid]['url'],
                        "followers": followers,
                        "popularity": a_obj.get('popularity', 0),
                        "id": aid
                    })
                    artists_already_found.add(aid)

            if len(final_results) >= 10: break

    app.logger.info(f"Scan complete. Found {len(final_results)} matches.")
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