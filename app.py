import os
import base64
import time
import random
import string
import re
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS
import requests
import logging

# --- Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# --- CREDENTIALS ---
# Make sure these are set in your environment variables!
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
MAX_FOLLOWERS = 250000 
REQUIRE_IMAGE = True
SEARCH_YEAR_RANGE = "2024-2025"

# BLACKLIST: Immediate rejection if these appear in the copyright line
MAJOR_LABELS = [
    "sony", "universal", "umg", "warner", "atlantic", "columbia", "rca", 
    "interscope", "capitol", "republic", "def jam", "elektra", 
    "island records", "arista", "epic", "bad boy", "cash money", 
    "roc nation", "aftermath", "shady", "young money", "concord", "bmg",
    "kobalt", "ada", "300 entertainment", "empire", "utg", "create music group",
    "fuga", "the orchard", "ingrooves", "awal", "virgin", "ultra records"
]

# ALLOWED SUFFIXES: Common independent business structures
# We allow these if the REST of the name is an EXACT match to the artist
ALLOWED_SUFFIXES = [
    "records", "music", "entertainment", "llc", "inc", "productions", 
    "band", "group", "collective", "ent", "official", "publishing", "ltd",
    "media", "studio", "studios"
]

# --- Auth & Session ---
token_info = {'access_token': None, 'expires_at': 0}
spotify_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
spotify_session.mount('https://', adapter)

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
    
    # Rate limit protection
    now = time.time()
    if now - last_request_time < 0.05:
        time.sleep(0.05)
    
    try:
        last_request_time = time.time()
        response = spotify_session.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 2))
            app.logger.warning(f"Rate limited. Sleeping {retry_after}s")
            time.sleep(retry_after)
            return make_request(url, token, params) 
        
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        app.logger.error(f"Request Error: {e}")
        return None

def normalize_text(text):
    """
    Removes years, special chars, and standardizes text for comparison.
    Example: "(C) 2025 The Band LLC" -> "thebandllc"
    """
    if not text: return ""
    # Remove standard copyright symbols and years (4 digits)
    text = re.sub(r'[\u00A9\u2117\(\)CPcp]', '', text) # Remove ©, ℗, (C), (P)
    text = re.sub(r'\b\d{4}\b', '', text) # Remove Year
    
    clean = text.lower().strip()
    # Remove "the " prefix for better matching
    if clean.startswith("the "): clean = clean[4:]
    # Remove non-alphanumeric (spaces, punctuation)
    return re.sub(r'[^a-z0-9]', '', clean)

def check_copyright_match(album, artist_name):
    """
    STRICT LOGIC:
    1. Rejects if Major Label is found.
    2. Accepts if 'Records DK' is found (DistroKid).
    3. Accepts if Normalized Copyright == Normalized Artist Name.
    """
    if not artist_name: return False
    
    # Retrieves both C (Composition) and P (Phonographic) lines
    copyrights = [c.get('text', '').lower() for c in album.get('copyrights', []) if c.get('text')]
    if not copyrights: return False

    # 1. MAJOR LABEL FILTER (Fast Fail)
    for text in copyrights:
        for major in MAJOR_LABELS:
            if major in text:
                return False

    # 2. MATCHING LOGIC
    artist_clean = normalize_text(artist_name)
    
    for text in copyrights:
        # A: Check for "Records DK" (DistroKid specific tag)
        if "records dk" in text:
            return True
            
        # B: Check for Exact Artist Name Match
        text_clean = normalize_text(text)
        
        # Exact match (e.g. "Artist Name" == "Artist Name")
        if artist_clean == text_clean:
            return True
            
        # Suffix match (e.g. "Artist Name LLC" or "Artist Name Records")
        if text_clean.startswith(artist_clean):
            suffix = text_clean[len(artist_clean):]
            if suffix in ALLOWED_SUFFIXES:
                return True
                
    return False

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: 
        return jsonify({"artists": []}), 500
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    final_results = []
    
    # STRATEGY: Randomize search to avoid hitting the same "Top 50" every time.
    search_char = random.choice(string.ascii_lowercase)
    # Search for albums released in 2024-2025. This ensures "Most Recent Release" relevance.
    query = f"{search_char} year:{SEARCH_YEAR_RANGE}" 
    offset = random.randint(0, 900)
    
    app.logger.info(f"Scanning Query: '{query}' | Offset: {offset}")

    # 1. Search for Albums
    search_url = 'https://api.spotify.com/v1/search'
    search_params = {
        'q': query,
        'type': 'album',
        'limit': 50,
        'offset': offset,
        'market': 'US'
    }
    
    search_data = make_request(search_url, token, search_params)
    
    if not search_data or 'albums' not in search_data:
        return jsonify({"artists": []})
        
    raw_albums = search_data['albums'].get('items', [])
    if not raw_albums:
        return jsonify({"artists": []})

    # 2. Batch Fetch Album Details (Copyrights are NOT in the search result)
    album_ids = [alb['id'] for alb in raw_albums if alb.get('id')]
    candidates = {} 

    # We batch these in groups of 20 (Spotify limit for 'Get Several Albums')
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details_url = 'https://api.spotify.com/v1/albums'
        details = make_request(details_url, token, {'ids': ','.join(chunk)})
        
        if not details: continue
        
        for album in details.get('albums', []):
            if not album: continue
            if not album.get('artists'): continue
            
            primary_artist = album['artists'][0]
            artist_id = primary_artist.get('id')
            artist_name = primary_artist.get('name')
            
            if artist_id in artists_already_found: continue
            if artist_id in candidates: continue
            
            # STRICT COPYRIGHT CHECK
            if check_copyright_match(album, artist_name):
                candidates[artist_id] = {
                    "name": artist_name,
                    "id": artist_id,
                    "release_date": album.get('release_date')
                }

    # 3. Final Verification: Check Artist Followers
    candidate_ids = list(candidates.keys())
    
    if candidate_ids:
        artists_url = 'https://api.spotify.com/v1/artists'
        for i in range(0, len(candidate_ids), 50): # Batch 50 artists
            chunk = candidate_ids[i:i+50]
            artists_data = make_request(artists_url, token, {'ids': ','.join(chunk)})
            
            if not artists_data: continue
            
            for artist in artists_data.get('artists', []):
                if not artist: continue
                
                followers = artist.get('followers', {}).get('total', 0)
                
                if followers < MAX_FOLLOWERS:
                    if REQUIRE_IMAGE and not artist.get('images'): continue
                    
                    cand = candidates[artist['id']]
                    final_results.append({
                        "name": cand['name'],
                        "url": artist.get('external_urls', {}).get('spotify'),
                        "followers": followers,
                        "popularity": artist.get('popularity', 0),
                        "id": artist['id']
                    })
                    
                    if len(final_results) >= 10: 
                        break
            if len(final_results) >= 10: 
                break

    app.logger.info(f"   -> Found {len(final_results)} matching artists.")
    return jsonify({"artists": final_results})

@app.route('/')
def serve_frontend():
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            return make_response(f.read())
    except FileNotFoundError:
        return "Error: spotify_scanner.html not found.", 404

if __name__ == '__main__':
    # Get the PORT from Render's environment variables (default to 5000 locally)
    port = int(os.environ.get("PORT", 5000))
    # host='0.0.0.0' is required for the app to be visible to Render
    app.run(host='0.0.0.0', port=port)