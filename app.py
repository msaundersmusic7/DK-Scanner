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

# We explicitly block Major Labels to ensure the "Artist Name" match 
# doesn't accidentally pick up "© 2025 Taylor Swift" (who is Major).
MAJOR_LABELS = ["sony", "universal", "warner", "atlantic", "columbia", "rca", "interscope", "capitol", "republic", "def jam", "elektra"]

# --- Auth ---
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
            time.sleep(2)
            return None 
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def check_copyright_match(album, artist_name):
    """
    Returns True if the copyright line matches:
    1. 'Records DK' / 'DistroKid'
    2. The Artist's exact name (indicating self-release)
    """
    if not artist_name: return False
    
    # Prepare Artist Name: Remove "The " and special chars for comparison
    artist_clean = artist_name.lower()
    if artist_clean.startswith("the "): artist_clean = artist_clean[4:]
    artist_clean = re.sub(r'[^a-z0-9]', '', artist_clean)
    
    # Regex for DistroKid specific text
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for copyright in album.get('copyrights', []):
        text = copyright.get('text', '')
        if not text: continue
        
        # 1. Safety Check: If it says "Sony" or "Warner", it's NOT independent.
        text_lower = text.lower()
        if any(major in text_lower for major in MAJOR_LABELS):
            return False

        # 2. Check for "Records DK" or "DistroKid"
        if dk_regex.search(text):
            return True
        
        # 3. Check for Artist Name match (Self-released)
        # We strip the copyright text to just letters/numbers
        # Example: "© 2025 Independent Boy" -> "2025independentboy"
        text_clean = re.sub(r'[^a-z0-9]', '', text_lower)
        
        # Does "independentboy" exist inside "2025independentboy"? YES.
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
    max_attempts = 15 # Scan up to 15 pages of results if needed
    
    while len(final_results) < 5 and attempts < max_attempts:
        attempts += 1
        
        # --- SIMPLE SEARCH STRATEGY ---
        # "year:2024-2025" gives us all recent music.
        # "offset" gives us a random slice of that list (0-950).
        offset = random.randint(0, 950)
        query = "year:2024-2025" 
        
        app.logger.info(f"Scanning 'Recent Releases' | Offset: {offset}")

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
        
        if not search_data: continue
        
        raw_albums = search_data.get('albums', {}).get('items', [])
        if not raw_albums: continue
        
        # 2. GET COPYRIGHTS (The Filter)
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates = {} 
        
        # Chunk requests to stay efficient
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

                # THE CORE CHECK: Does Copyright contain "Records DK" or "Artist Name"?
                if check_copyright_match(album, name):
                    candidates[aid] = {
                        'name': name,
                        'url': primary_artist.get('external_urls', {}).get('spotify')
                    }

        if not candidates: continue

        # 3. GET FOLLOWERS (The Quality Check)
        artist_ids = list(candidates.keys())
        for i in range(0, len(artist_ids), 50):
            chunk = artist_ids[i:i+50]
            adata = make_request('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
            if not adata: continue
            
            for a_obj in adata.get('artists', []):
                if not a_obj: continue
                
                followers = a_obj.get('followers', {}).get('total', 0)
                
                # Basic Quality Filters
                if followers > MAX_FOLLOWERS: continue
                if REQUIRE_IMAGE and not a_obj.get('images'): continue
                
                aid = a_obj.get('id')
                if aid in candidates:
                    app.logger.info(f"FOUND: {candidates[aid]['name']} ({followers} followers)")
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