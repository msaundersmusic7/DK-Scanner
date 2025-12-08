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

# --- REFINEMENT SETTINGS (RELAXED) ---
MAX_FOLLOWERS = 50000      
MIN_POPULARITY = 2         # Lowered slightly to catch new talent
REQUIRE_IMAGE = True       
REQUIRE_GENRE = True       
MIN_RELEASES = 1           # CHANGED: 1 release is okay (new artists)
DAYS_WINDOW = 180          # CHANGED: Extended to 6 months (Indies release slower)

# --- REFINED ANTI-BOT KEYWORDS ---
# Only blocking OBVIOUS non-artist content. 
# Removed: "beats", "chill", "focus", "study" (Real producers use these)
BLOCKED_KEYWORDS = [
    "white noise", "pink noise", "brown noise",
    "sleep sounds", "deep sleep", "meditation", 
    "lullaby", "therapy", "yoga", "spa", 
    "binaural", "frequencies", "chakra", "healing",
    "nature sounds", "rain sounds", "fan noise"
]

P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

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

def check_copyright_match(album, artist_name=None):
    # Returns a tuple: (Match Found?, Is It Specifically DK?)
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            if P_LINE_REGEX.search(text): 
                return (True, True) # Strict DK Match
            if artist_name and artist_name.lower() in text: 
                return (True, False) # Name Match
    return (False, False)

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
    Worker function.
    """
    aid, name, url, pop, followers, token, initial_dk_match = package
    
    # If we found a STRICT 'Records DK' match initially, we are lenient on activity.
    is_vip_match = initial_dk_match 

    api_url = f"https://api.spotify.com/v1/artists/{aid}/albums"
    params = {'include_groups': 'album,single', 'limit': 20, 'market': 'US'}
    data = make_request_with_token(api_url, token, params)
    
    if not data or not data.get('items'): return None
    items = data.get('items', [])
    
    # Relaxed Filter: Allow 1 release if it's a VIP match or new artist
    if len(items) < MIN_RELEASES and not is_vip_match: return None

    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest_release = items[0]
    
    # Relaxed Filter: Recency
    if not is_vip_match: # VIPs skip the date check (we want them regardless)
        release_date_str = latest_release.get('release_date', '2000-01-01')
        try:
            if len(release_date_str) == 4: r_date = datetime.datetime.strptime(release_date_str, "%Y")
            elif len(release_date_str) == 7: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m")
            else: r_date = datetime.datetime.strptime(release_date_str, "%Y-%m-%d")
            
            if (datetime.datetime.now() - r_date).days > DAYS_WINDOW:
                return None
        except ValueError:
            return None

    # Check Copyright on LATEST release to confirm current status
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_release['id']}"
    full_album = make_request_with_token(album_details_url, token)
    
    if full_album:
        match_found, _ = check_copyright_match(full_album, name)
        if match_found:
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
    
    # 1. Random Search
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

    # 2. Batch Copyright Check
    candidate_map = {} 
    
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details = make_request_with_token('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
        if not details: continue
        
        for album in details.get('albums', []):
            if not album: continue
            
            # Check copyright logic
            dk_match, is_strict_dk = check_copyright_match(album, None)
            
            for artist in album.get('artists', []):
                aid = artist.get('id')
                name = artist.get('name')
                
                if aid and name and aid not in artists_already_found and is_real_artist_name(name):
                    # Logic: If global DK match, OR artist name match
                    name_match, _ = check_copyright_match(album, name)
                    
                    if dk_match or name_match:
                        candidate_map[aid] = {
                            'name': name, 
                            'url': artist.get('external_urls', {}).get('spotify'),
                            'initial_dk_match': is_strict_dk
                        }

    if not candidate_map: return jsonify({"artists": []})

    # 3. Batch Artist Details (Popularity/Image Filter)
    artist_ids = list(candidate_map.keys())
    verified_candidates = [] 
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        adata = make_request_with_token('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
        if not adata: continue
        
        for a_obj in adata.get('artists', []):
            if not a_obj: continue
            if is_quality_candidate(a_obj):
                aid = a_obj.get('id')
                verified_candidates.append((
                    aid,
                    candidate_map[aid]['name'],
                    candidate_map[aid]['url'],
                    a_obj.get('popularity', 0),
                    a_obj.get('followers', {}).get('total', 0),
                    token,
                    candidate_map[aid]['initial_dk_match']
                ))

    # 4. Threaded Verification
    final_results = []
    if verified_candidates:
        with ThreadPoolExecutor(max_workers=10) as executor:
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