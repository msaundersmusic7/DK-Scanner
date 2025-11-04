import os
import requests
import base64
import time
# Import 'send_from_directory'
from flask import Flask, jsonify, make_response, send_from_directory
from flask_cors import CORS

# Initialize Flask App
# We specify 'static_folder=None' because we are serving the HTML from the root
app = Flask(__name__, static_folder=None)
CORS(app)

# --- Spotify API Logic ---

def get_access_token():
    """
    Exchanges your Client ID and Secret for a temporary Access Token.
    Loads credentials securely from environment variables.
    """
    client_id = os.environ.get('SPOTIFY_CLIENT_ID')
    client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')

    if not client_id or not client_secret:
        print("Error: SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set.")
        return None

    auth_url = 'https://community.spotify.com/t5/Accounts/Changing-my-country/td-p/46819755'
    auth_string = f"{client_id}:{client_secret}"
    auth_bytes = auth_string.encode('utf-8')
    auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')

    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(auth_url, headers=headers, data=data)
        response.raise_for_status()
        return response.json().get('access_token')
    except Exception as e:
        print(f"Error getting access token: {e}")
        return None

def find_artists(token):
    """
    Searches Spotify for all albums matching 'Records DK' and finds the artists.
    """
    artists_found = set()
    total_albums_scanned = 0
    
    search_url = 'https://www.google.com/search?q=http.googleusercontent.com/spotify.com/31'
    auth_header = {"Authorization": f"Bearer {token}"}

    print("Starting server-side scan...")

    while search_url:
        try:
            response = requests.get(search_url, headers=auth_header)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                print(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            data = response.json()
            albums = data.get('albums', {}).get('items', [])
            
            if not albums:
                break

            total_albums_scanned += len(albums)
            print(f"Scanned {total_albums_scanned} albums...")

            for album in albums:
                for copyright in album.get('copyrights', []):
                    if copyright.get('type') == 'P' and 'DK' in copyright.get('text', ''):
                        for artist in album.get('artists', []):
                            if artist.get('name'):
                                artists_found.add(artist.get('name'))
                        break
            
            search_url = data.get('albums', {}).get('next')
            time.sleep(0.1) # Be nice to the API

        except Exception as e:
            print(f"Error during search: {e}")
            break

    print(f"Scan complete. Found {len(artists_found)} artists.")
    return sorted(list(artists_found))


# --- API Endpoint ---
@app.route('/api/scan', methods=['GET'])
def start_scan():
    """
    This is the public endpoint your frontend will call.
    """
    print("Received scan request...")
    
    # 1. Get token
    token = get_access_token()
    if not token:
        # Use make_response to set the 500 status code
        return make_response(jsonify({"error": "Authentication failed. Server credentials may be missing."}), 500)
    
    # 2. Find artists
    artists = find_artists(token)
    
    # 3. Return the list as JSON
    return jsonify({"artists": artists})

# --- Frontend Route ---
@app.route('/')
def serve_frontend():
    """
    Serves the main spotify_scanner.html file from the root directory.
    """
    # This tells Flask to send the file 'spotify_scanner.html' 
    # from the current directory ('.')
    return send_from_directory('.', 'spotify_scanner.html')


if __name__ == '__main__':
    # Render will use the 'gunicorn' command instead of this.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

