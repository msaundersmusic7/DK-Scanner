import os
import base64
import time
import random
import string
import re
import datetime 
# --- FIX IS HERE: Added 'make_response' back to imports ---
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)
# Switch logging to DEBUG so you can see exactly why artists are rejected in Render logs
logging.basicConfig(level=logging.DEBUG) 

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# --- SETTINGS ---
MAX_FOLLOWERS = 50000      
MIN_POPULARITY = 0         # Catch everyone, even brand new artists
REQUIRE_IMAGE = True       
DAYS_WINDOW = 365          # 1 Year Window (Safe for Indies)
MIN_RELEASES = 1

# --- BOT FILTER (Minimal) ---
BLOCKED_KEYWORDS = ["white noise", "sleep", "meditation", "lullaby", "frequency", "rain sounds"]

# Regex: Matches "Records DK", "DK <number>", "DistroKid"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+|distrokid)", re.IGNORECASE)

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

def clean_name_for_match(name):
    """Simplifies string for copyright comparison."""
    if not name: return ""
    # Remove "The", punctuation, and lower case
    clean = name.lower()
    if clean.startswith("the "): clean = clean[4:]
    return re.sub(r'[^a-z0-9]', '', clean)

def check_copyright_match(album, artist_name=None):
    """
    Returns True if:
    1. 'Records DK' / 'DistroKid' is in the text.
    2. The Artist's name is inside the copyright text.
    """
    for copyright in album.get('copyrights', []):
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            text_clean = re.sub(r'[^a-z0-9]', '', text) # Clean copyright text
            
            # 1. Regex Match (Records DK)
            if P_LINE_REGEX.search(text): 
                return True
            
            # 2. Name Match
            if artist_name:
                name_clean = clean_name_for_match(artist_name)
                # Check if "bandname" is inside "2025bandnamellc"
                if len(name_clean) > 3 and name_clean in text_clean:
                    return True
    return False

def is_real_artist_name(name):
    name_lower = name.lower()
    for word in BLOCKED_KEYWORDS:
        if word in name_lower: return False
    return True

def process_search_query(token, artists_already_found):
    """
    Runs ONE search query and returns potential candidates.
    We pulled this out to allow looping/retrying.
    """
    current_year = datetime.datetime.now().year
    
    # STRATEGY: 2 Random Letters (e.g., "st*", "ba*")
    # This creates 676 unique buckets. Much safer than offsets.
    char1 = random.choice(string.ascii_lowercase)
    char2 = random.choice(string.ascii_lowercase)
    query = f"{char1}{char2}* year:{current_year}"
    
    app.logger.info(f"Running Query: {query}")

    search_data = make_request_with_token(
        'https://api.spotify.com/v1/search', token,
        {'q': query, 'type': 'album', 'limit': 50, 'market': 'US'}
    )
    
    if not search_data: return []
    
    raw_albums = search_data.get('albums', {}).get('items', [])
    if not raw_albums: 
        app.logger.info("Query returned 0 albums.")
        return []
    
    # Batch Get Album Details (To check copyrights)
    album_ids = [alb['id'] for alb in raw_albums if alb and alb.get('id')]
    candidate_map = {} 
    
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details = make_request_with_token('https://api.spotify.com/v1/albums', token, {'ids': ','.join(chunk)})
        if not details: continue
        
        for album in details.get('albums', []):
            if not album: continue
            
            # 1. Global DK Check
            is_dk = check_copyright_match(album, None)
            
            for artist in album.get('artists', []):
                aid = artist.get('id')
                name = artist.get('name')
                
                if aid and name and aid not in artists_already_found and is_real_artist_name(name):
                    # 2. Name Match Check
                    is_self_release = check_copyright_match(album, name)
                    
                    if is_dk or is_self_release:
                        candidate_map[aid] = {
                            'name': name,
                            'url': artist.get('external_urls', {}).get('spotify')
                        }

    # Batch Get Artist Details (To check followers/image)
    artist_ids = list(candidate_map.keys())
    verified = []
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        adata = make_request_with_token('https://api.spotify.com/v1/artists', token, {'ids': ','.join(chunk)})
        if not adata: continue
        
        for a_obj in adata.get('artists', []):
            if not a_obj: continue
            
            if REQUIRE_IMAGE and not a_obj.get('images'): continue
            if a_obj.get('followers', {}).get('total', 0) > MAX_FOLLOWERS: continue
            
            aid = a_obj.get('id')
            if aid in candidate_map:
                verified.append({
                    "name": candidate_map[aid]['name'],
                    "url": candidate_map[aid]['url'],
                    "followers": a_obj.get('followers', {}).get('total', 0),
                    "popularity": a_obj.get('popularity', 0),
                    "id": aid
                })
    
    return verified

def verify_latest_release(artist, token):
    """
    Final Check: Is their *latest* release actually recent?
    """
    aid = artist['id']
    name = artist['name']
    
    api_url = f"https://api.spotify.com/v1/artists/{aid}/albums"
    params = {'include_groups': 'album,single', 'limit': 5, 'market': 'US'}
    data = make_request_with_token(api_url, token, params)
    
    if not data or not data.get('items'): return None
    items = data.get('items', [])
    
    # Sort by date
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    latest = items[0]
    
    # Recency Check
    r_date_str = latest.get('release_date', '2000-01-01')
    try:
        if len(r_date_str) == 4: r_date = datetime.datetime.strptime(r_date_str, "%Y")
        else: r_date = datetime.datetime.strptime(r_date_str, "%Y-%m-%d" if len(r_date_str) == 10 else "%Y-%m")
        
        if (datetime.datetime.now() - r_date).days > DAYS_WINDOW:
            return None # Too old
    except:
        return None # Date error, skip

    # Re-verify Copyright on Latest
    full_album = make_request_with_token(f"https://api.spotify.com/v1/albums/{latest['id']}", token)
    if full_album and check_copyright_match(full_album, name):
        return artist # Success!
        
    return None

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    token = get_spotify_token()
    if not token: return jsonify({"artists": []})
    
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    final_results = []
    attempts = 0
    
    # AUTO-RETRY LOOP
    # If a search returns 0 results, we try again immediately (up to 5 times)
    # This guarantees the frontend gets data.
    while len(final_results) < 3 and attempts < 5:
        attempts += 1
        candidates = process_search_query(token, artists_already_found)
        
        if not candidates: continue
        
        # Run detailed verification in threads
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_artist = {executor.submit(verify_latest_release, c, token): c for c in candidates}
            for future in as_completed(future_to_artist):
                res = future.result()
                if res:
                    final_results.append(res)
                    artists_already_found.add(res['id']) # Prevent duplicates in same batch

    app.logger.info(f"Returning {len(final_results)} artists after {attempts} attempts.")
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