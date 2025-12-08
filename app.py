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

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.DEBUG)

# --- Configuration ---
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# FILTER SETTINGS
MAX_FOLLOWERS = 100000     
MIN_POPULARITY = 2         
REQUIRE_IMAGE = True       
REQUIRE_GENRE = True       

# Regex: Matches "Records DK" or "DK <number>"
P_LINE_REGEX = re.compile(r"(records\s+dk|dk\s+\d+)", re.IGNORECASE)

# --- Global Token Cache ---
token_info = {
    'access_token': None,
    'expires_at': 0
}

spotify_session = requests.Session()

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
    
    headers = {
        "Authorization": f"Basic {auth_base64}", 
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()
        json_data = response.json()
        
        token_info['access_token'] = json_data.get('access_token')
        token_info['expires_at'] = now + json_data.get('expires_in', 3600)
        return token_info['access_token']
    except Exception as e:
        app.logger.error(f"Auth Error: {e}")
        return None

def make_spotify_request(url, params=None):
    token = get_spotify_token()
    if not token: return None
    
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = spotify_session.get(url, headers=headers, params=params)
        
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 5))
            time.sleep(retry_after)
            return make_spotify_request(url, params)
            
        response.raise_for_status()
        return response.json()
    except Exception as e:
        app.logger.error(f"Request Error ({url}): {e}")
        return None

def check_copyright_match(album, artist_name=None):
    """
    Checks if any C or P line matches:
    1. The 'Records DK' regex
    2. The artist's name (if provided)
    """
    copyrights = album.get('copyrights', [])
    if not copyrights:
        return False

    for copyright in copyrights:
        if copyright.get('type') in ['P', 'C']:
            text = copyright.get('text', '').lower()
            
            # 1. Check for Records DK
            if P_LINE_REGEX.search(text):
                return True
            
            # 2. Check for Artist Name (if provided)
            # We look for the artist name inside the copyright text
            if artist_name:
                # Basic check: is "artist name" inside "text"?
                # e.g. Artist: "Russ", Text: "DIEMON/Russ" -> Match
                if artist_name.lower() in text:
                    return True
                    
    return False

def is_quality_candidate(artist_obj):
    if REQUIRE_IMAGE:
        images = artist_obj.get('images', [])
        if not images: return False

    if REQUIRE_GENRE:
        genres = artist_obj.get('genres', [])
        if not genres: return False

    if artist_obj.get('popularity', 0) < MIN_POPULARITY:
        return False
        
    return True

def verify_artist_latest_release(artist_id, artist_name):
    """
    Fetches artist's albums, sorts by date.
    Checks if the NEWEST release matches 'Records DK' OR 'Artist Name'.
    """
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    params = {'include_groups': 'album,single', 'limit': 10, 'market': 'US'}
    
    data = make_spotify_request(url, params)
    if not data or not data.get('items'): return False
    
    items = data.get('items', [])
    # Sort by release_date descending
    items.sort(key=lambda x: x.get('release_date', '0000'), reverse=True)
    
    latest_album_id = items[0].get('id')
    
    # Check Copyright of the latest album
    album_details_url = f"https://api.spotify.com/v1/albums/{latest_album_id}"
    full_album = make_spotify_request(album_details_url)
    
    if full_album and check_copyright_match(full_album, artist_name):
        return True
        
    return False

@app.route('/api/scan_one_page', methods=['POST'])
def scan_one_page():
    data = request.get_json()
    artists_already_found = set(data.get('artists_already_found', []))
    
    # 1. Random Search
    current_year = datetime.datetime.now().year
    char1 = random.choice(string.ascii_lowercase)
    char2 = random.choice(string.ascii_lowercase)
    query = f"{char1}{char2}* year:{current_year}"
    
    search_data = make_spotify_request(
        'https://api.spotify.com/v1/search',
        params={'q': query, 'type': 'album', 'limit': 50, 'offset': 0, 'market': 'US'}
    )
    
    if not search_data: return jsonify({"artists": []})
    
    album_ids = [item['id'] for item in search_data.get('albums', {}).get('items', []) if item]
    if not album_ids: return jsonify({"artists": []})

    # 2. Batch Fetch Album Details
    candidate_artists = {}
    
    for i in range(0, len(album_ids), 20):
        chunk = album_ids[i:i+20]
        details_data = make_spotify_request(
            'https://api.spotify.com/v1/albums',
            params={'ids': ','.join(chunk)}
        )
        
        if not details_data: continue
        
        for album in details_data.get('albums', []):
            if not album: continue
            
            # Check global "Records DK" match first (saves time)
            has_dk_match = check_copyright_match(album, artist_name=None)
            
            for artist in album.get('artists', []):
                aid = artist.get('id')
                name = artist.get('name')
                
                if aid and name and aid not in artists_already_found:
                    # If the album matches DK regex, OR this specific artist's name is in the copyright
                    if has_dk_match or check_copyright_match(album, artist_name=name):
                        candidate_artists[aid] = {
                            'name': name,
                            'url': artist.get('external_urls', {}).get('spotify')
                        }

    if not candidate_artists:
        return jsonify({"artists": []})

    # 3. Validation Phase
    final_artists = []
    artist_ids = list(candidate_artists.keys())
    
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        artists_data = make_spotify_request(
            'https://api.spotify.com/v1/artists', 
            params={'ids': ','.join(chunk)}
        )
        
        if not artists_data: continue

        for artist_obj in artists_data.get('artists', []):
            if not artist_obj: continue
            
            aid = artist_obj.get('id')
            followers = artist_obj.get('followers', {}).get('total', 0)
            
            if followers < MAX_FOLLOWERS and is_quality_candidate(artist_obj):
                
                # Pass the artist name to verify the latest release
                name = candidate_artists[aid]['name']
                if verify_artist_latest_release(aid, name):
                    final_artists.append({
                        "name": name,
                        "url": candidate_artists[aid]['url'],
                        "followers": followers,
                        "popularity": artist_obj.get('popularity', 0),
                        "id": aid
                    })

    return jsonify({"artists": final_artists})

@app.route('/')
def serve_frontend():
    try:
        with open('spotify_scanner.html', 'r', encoding='utf-8') as f:
            return make_response(f.read())
    except FileNotFoundError:
        return "Error: frontend not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
