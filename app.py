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
logging.basicConfig(level=logging.INFO) # Switched to INFO to reduce log noise

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- REFINEMENT SETTINGS ---
MAX_FOLLOWERS = 50000      
MIN_POPULARITY = 5         
REQUIRE_IMAGE = True       
REQUIRE_GENRE = True       
MIN_RELEASES = 2           
DAYS_WINDOW = 90           

# Anti-Bot Keywords
BLOCKED_KEYWORDS = [
    "sleep", "relax", "meditation", "lullaby", "noise", "rain", 
    "nature", "sounds", "therapy", "yoga", "spa", "study", "beats", 
    "lofi", "chill", "focus", "binaural", "frequencies", "baby"
]

P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

# --- Global Token Cache ---
token_info = {'access_token': None, 'expires_at': 0}
# Session for connection pooling (Thread-safe-ish)
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
    """Helper that takes a token explicitly (better for threads)"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = spotify_session.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 429:
            # Don't sleep in threads if possible, just fail this one or short sleep
            return None 
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def check_copyright_match(album, artist_name=None):
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            if P_LINE_REGEX.search(text): return True
            if artist_name and artist_name.lower() in text: return True
    return False

def is_real_artist_name(name):
    name_lower = name.lower()
    for word in BLOCKED_KEYWORDS:
        if word in name_lower: return False
    return True

def is_quality_candidate(artist_obj):
    if REQUIRE_IMAGE and not artist_obj.get('images'): return False
    if REQUIRE_GENRE and not artist_obj.get('genres'): return False
    if artist_obj.get('popularity', 0) < MIN_POPULARITY: return False
    if not is_real_artist_name(artist_obj.get('name', '')): return False
    return True

def check_single_artist_activity(package):
    """
    Worker function for ThreadPool.
    Accepts a tuple: (artist_id, artist_name, artist_url, artist_pop, artist_followers, token)
    Returns: Artist Dict or None
    """
    aid, name, url, pop, followers, token = package
    
    # 1. Fetch Albums
    api_url = f"https://api.spotify.com/v1/artists/{aid}/albums"
    params = {'include_groups': 'album,single', 'limit': 20, 'market': 'US'}
    data = make_request_with_token(api_url, token, params)
    
    if not data or not data.get('items'): return None
    items = data.get('items', [])
    
    # Filter: Catalog Depth
    if len(items) < MIN_RELEASES: return None

    # Filter: Recency
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest_release = items[0]
    release_date_str = latest_release.get('release_date', '2000-01-01')
    
    try:
        if len(release_date_str) == 4: r_date = datetime.datetime.strptime(release_date_str, "%Y")
        elif len(release_date_str) == 7: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m")
        else: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m-%d")
        
        if (datetime.datetime.now() - r_date).days > DAYS_WINDOW:
            return None
    except ValueError:
        return None

    # Filter: Copyright on LATEST release
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_release['id']}"
    full_album = make_request_with_token(album_details_url, token)
    
    if full_album and check_copyright_match(full_album, name):
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
    # 1. Setup
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    # 2. Random Search
    current_year = datetime.datetime.now().year
    char1 = random.choice(string.ascii_lowercase)
    char2 = random.choice(string.ascii_lowercase)
    query = f"{char1}{char2}* year:{current_year}"
    
    search_data = make_request_with_token(
        'https://api.spotify.com/v1/search', token,
        {'q': query, 'type': 'album', 'limit': 50, 'offset': 0, 'market': 'US'}
    )
    
    if not search_data: return jsonify({"artists": []})
    album_ids = [item['id'] for item in search_data.get('albums', {}).get('items', []) if item]
    if not album_ids: return jsonify({"artists": []})

    # 3. Batch Get Album Details (to filter by Copyright first)
    # We do this sequentially because we need to build the candidate list
    candidate_map = {} # aid -> {name, url}
    
    # Process 50 albums in 3 chunks (20, 20, 10)
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details = make_request_with_token('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
        if not details: continue
        
        for album in details.get('albums', []):
            if not album: continue
            
            # Fast Check: Global DK Match
            has_dk = check_copyright_match(album, None)
            
            for artist in album.get('artists', []):
                aid = artist.get('id')
                name = artist.get('name')
                
                # Preliminary checks (Name blacklist, duplicates)
                if aid and name and aid not in artists_already_found and is_real_artist_name(name):
                    if has_dk or check_copyright_match(album, name):
                        candidate_map[aid] = {
                            'name': name, 
                            'url': artist.get('external_urls', {}).get('spotify')
                        }

    if not candidate_map: return jsonify({"artists": []})

    # 4. Batch Get Artist Details (to filter by Popularity/Image)
    # This is fast because we can request 50 artists at once
    artist_ids = list(candidate_map.keys())
    verified_candidates = [] # List of tuples for the thread worker
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        adata = make_request_with_token('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
        if not adata: continue
        
        for a_obj in adata.get('artists', []):
            if not a_obj: continue
            if is_quality_candidate(a_obj):
                aid = a_obj.get('id')
                # Prepare data for the expensive check
                verified_candidates.append((
                    aid,
                    candidate_map[aid]['name'],
                    candidate_map[aid]['url'],
                    a_obj.get('popularity', 0),
                    a_obj.get('followers', {}).get('total', 0),
                    token # Pass token to worker
                ))

    # 5. Parallel "Recency" Verification
    # This is the secret sauce for speed. We check all candidates at once.
    final_results = []
    
    if verified_candidates:
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Submit all tasks
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