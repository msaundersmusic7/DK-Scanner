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

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
# Lowered max followers to target true indies/startups
MAX_FOLLOWERS = 50000      
REQUIRE_IMAGE = True        
BLOCKED_KEYWORDS = ["white noise", "sleep", "lullaby", "rain sounds", "meditation", "frequency", "karaoke", "tribute"]

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
            app.logger.warning("Rate limit hit (429).")
            return None 
        response.raise_for_status()
        return response.json()
    except Exception as e:
        # app.logger.error(f"Request failed: {e}")
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
    2. The Artist's name is inside the copyright text (Common for DistroKid).
    """
    artist_clean = clean_name_for_match(artist_name)
    
    # Regex to catch explicit DistroKid markers
    # Matches: "Records DK", "DistroKid.com", "DistroKid", "DK"
    dk_regex = re.compile(r"(records\s*dk|distrokid|dk\s*\d+)", re.IGNORECASE)

    for copyright in album.get('copyrights', []):
        text = copyright.get('text', '').lower()
        
        # 1. Check for explicit DistroKid string
        if dk_regex.search(text): 
            # app.logger.info(f"MATCH (Explicit DK): {text}")
            return True
        
        # 2. Check for Artist Name match (Self-released via DK often looks like this)
        # We strip special chars to match "The Band!" with "2024 The Band"
        text_clean = re.sub(r'[^a-z0-9]', '', text)
        
        # Security check: Ensure artist name isn't too short to avoid false positives
        if len(artist_clean) >= 3 and artist_clean in text_clean:
            # Verify it's not a major label owned copyright by checking for major keywords
            if "sony" not in text and "universal" not in text and "warner" not in text:
                # app.logger.info(f"MATCH (Name Match): {artist_name} in {text}")
                return True
                
    return False

def is_real_artist_name(name):
    if not name: return False
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
    
    # We try 3 random searches to find gold
    for attempt in range(3):
        if len(final_results) >= 5: break

        # --- STRATEGY: RANDOMIZED DEEP DIVE ---
        # We pick a random character (e.g., 'a') and a random offset.
        # This bypasses the "Popular" filter that blocks indie artists.
        char = random.choice(string.ascii_lowercase)
        
        # Use wildcard search + current years to find active artists
        query = f"{char}* year:2024-2025" 
        
        # Random offset (0-950) ensures we don't just get the most popular matches for 'a'
        offset = random.randint(0, 950) 
        
        app.logger.info(f"Scanning: Query='{query}' Offset={offset}")

        # 1. Search for Albums
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
        if not raw_albums: 
            app.logger.info("No albums returned from search.")
            continue
        
        # 2. Get Album Details (Copyrights are NOT in search results, must fetch details)
        album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
        candidates = {} 
        
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i:i+20]
            details = make_request('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
            if not details: continue
            
            for album in details.get('albums', []):
                if not album: continue
                
                # Check Copyrights FIRST (Efficiency)
                # We check matches against the FIRST artist on the album
                if not album.get('artists'): continue
                primary_artist = album['artists'][0]
                name = primary_artist.get('name')
                aid = primary_artist.get('id')

                if is_real_artist_name(name) and check_copyright_match(album, name):
                     if aid not in artists_already_found:
                        candidates[aid] = {
                            'name': name,
                            'url': primary_artist.get('external_urls', {}).get('spotify')
                        }
                # else:
                #    app.logger.info(f"Failed Copyright/Name Check: {name} | {album.get('copyrights')}")

        if not candidates: continue

        # 3. Get Artist Profiles (Check Followers)
        artist_ids = list(candidates.keys())
        for i in range(0, len(artist_ids), 50):
            chunk = artist_ids[i:i+50]
            adata = make_request('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
            if not adata: continue
            
            for a_obj in adata.get('artists', []):
                if not a_obj: continue
                
                followers = a_obj.get('followers', {}).get('total', 0)
                
                # FILTER: Followers
                if followers > MAX_FOLLOWERS:
                    # app.logger.info(f"REJECTED {a_obj['name']}: Too many followers ({followers})")
                    continue

                # FILTER: Image
                if REQUIRE_IMAGE and not a_obj.get('images'):
                    # app.logger.info(f"REJECTED {a_obj['name']}: No image")
                    continue
                
                aid = a_obj.get('id')
                if aid in candidates:
                    app.logger.info(f"SUCCESS: Found {candidates[aid]['name']} ({followers} followers)")
                    final_results.append({
                        "name": candidates[aid]['name'],
                        "url": candidates[aid]['url'],
                        "followers": followers,
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
        return "Error: spotify_scanner.html not found. Please upload it.", 404

if __name__ == '__main__':
    # Ensure you set these in your environment!
    # export SPOTIFY_CLIENT_ID="your_id"
    # export SPOTIFY_CLIENT_SECRET="your_secret"
    app.run(debug=True, port=5000)