import os
import base64
import time
import random
import re
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS
import requests
import logging

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
MAX_FOLLOWERS = 100000      # Higher limit to catch buzzing indies
REQUIRE_IMAGE = True        # Filter out low-effort spam
BLOCKED_KEYWORDS = ["white noise", "sleep", "lullaby", "rain sounds", "meditation", "frequency", "chakra", "binaural"]

# Regex for "Records DK" or "DistroKid"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+|distrokid)", re.IGNORECASE)

# --- Token Management ---
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

def make_request(url, token, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = spotify_session.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 429:
            return None 
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def clean_name_for_match(name):
    if not name: return ""
    clean = name.lower()
    if clean.startswith("the "): clean = clean[4:]
    return re.sub(r'[^a-z0-9]', '', clean)

def check_copyright_match(album, artist_name):
    """
    Returns True if:
    1. 'Records DK' / 'DistroKid' is in the text.
    2. The Artist's name is inside the copyright text.
    """
    artist_clean = clean_name_for_match(artist_name)
    
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            text_clean = re.sub(r'[^a-z0-9]', '', text) 
            
            # 1. Regex Match (Records DK)
            if P_LINE_REGEX.search(text): 
                return True
            
            # 2. Name Match (e.g. "© 2025 Band Name" matches "Band Name")
            if len(artist_clean) >= 3 and artist_clean in text_clean:
                return True
    return False

def is_real_artist_name(name):
    name_lower = name.lower()
    for word in BLOCKED_KEYWORDS:
        if word in name_lower: return False
    return True

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    final_results = []
    
    # We try up to 3 different offsets to find results
    for attempt in range(3):
        if len(final_results) >= 5: break
        
        # KEY CHANGE: Using 'tag:new' as the primary source
        # Offset max is usually 1000 for this tag. We pick a random spot.
        offset = random.randint(0, 900)
        
        search_data = make_request(
            'https://api.spotify.com/v1/search', token,
            {
                'q': 'tag:new', 
                'type': 'album', 
                'limit': 50, 
                'offset': offset, 
                'market': 'US'
            }
        )
        
        if not search_data: continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: continue
        
        # 1. Get Album IDs
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates = {} 
        
        # 2. Batch Fetch Full Details (for Copyrights)
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i:i+20]
            details = make_request('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
            if not details: continue
            
            for album in details.get('albums', []):
                if not album: continue
                
                for artist in album.get('artists', []):
                    aid = artist.get('id')
                    name = artist.get('name')
                    
                    if aid and name and aid not in artists_already_found and is_real_artist_name(name):
                        # CORE CHECK: Matches DK or Self-Release?
                        if check_copyright_match(album, name):
                            candidates[aid] = {
                                'name': name,
                                'url': artist.get('external_urls', {}).get('spotify')
                            }

        if not candidates: continue

        # 3. Batch Fetch Artist Profiles (for Image/Followers)
        artist_ids = list(candidates.keys())
        for i in range(0, len(artist_ids), 50):
            chunk = artist_ids[i:i+50]
            adata = make_request('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
            if not adata: continue
            
            for a_obj in adata.get('artists', []):
                if not a_obj: continue
                
                if REQUIRE_IMAGE and not a_obj.get('images'): continue
                if a_obj.get('followers', {}).get('total', 0) > MAX_FOLLOWERS: continue
                
                aid = a_obj.get('id')
                if aid in candidates:
                    final_results.append({
                        "name": candidates[aid]['name'],
                        "url": candidates[aid]['url'],
                        "followers": a_obj.get('followers', {}).get('total', 0),
                        "popularity": a_obj.get('popularity', 0),
                        "id": aid
                    })
                    artists_already_found.add(aid)

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