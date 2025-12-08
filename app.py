<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Label Scanner | Live Feed</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
        .spinner {
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-left-color: #ef4444; 
            border-radius: 50%;
            width: 20px;
            height: 20px;
            animation: spin 0.6s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #000; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #ef4444; }
        .glass-panel {
            background: rgba(20, 20, 20, 0.6);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
    </style>
</head>
<body class="bg-black text-white min-h-screen p-8 selection:bg-red-600 selection:text-white">
    <div class="max-w-7xl mx-auto">
        
        <div class="flex flex-col items-center justify-center mb-12 mt-8">
            <h1 class="text-4xl font-extrabold tracking-tight mb-2">NEW RELEASE <span class="text-red-600">SCANNER</span></h1>
            <p class="text-gray-500 text-sm font-medium uppercase">Feed: tag:new • Source: Spotify • Filter: Records DK / Self-Released</p>
        </div>

        <div class="flex flex-col items-center mb-10">
            <button id="start-scan" class="group relative bg-white text-black font-extrabold py-4 px-10 rounded-xl shadow-lg hover:shadow-[0_0_25px_rgba(255,255,255,0.3)] transition-all duration-300 transform hover:-translate-y-1 flex items-center overflow-hidden">
                <span id="button-text" class="tracking-wide text-lg">SCAN NEW RELEASES</span>
                <div id="loading-spinner" class="spinner ml-3 hidden"></div>
            </button>
            <div id="status" class="mt-4 text-gray-500 font-mono text-xs">Ready to intercept new uploads.</div>
        </div>

        <div id="results-container" class="glass-panel rounded-2xl p-1 hidden opacity-0 transition-opacity duration-500">
            <div class="flex justify-between items-center p-5 border-b border-white/5">
                <h2 class="text-sm font-bold uppercase tracking-wider text-gray-300">Live Results</h2>
                <div class="flex space-x-2">
                    <button id="download-results" class="bg-red-600 hover:bg-red-500 text-white px-5 py-2 rounded-lg text-xs font-bold shadow-lg shadow-red-900/20 transition-all hidden">Download Report</button>
                </div>
            </div>
            
            <div class="overflow-x-auto max-h-[600px]">
                <table class="w-full text-left">
                    <thead class="bg-black/40 sticky top-0 backdrop-blur-md z-10">
                        <tr>
                            <th class="px-6 py-4 text-xs font-bold text-gray-400 uppercase tracking-widest">Artist</th>
                            <th class="px-6 py-4 text-xs font-bold text-gray-400 uppercase tracking-widest">Followers</th>
                            <th class="px-6 py-4 text-xs font-bold text-gray-400 uppercase tracking-widest">Score</th>
                            <th class="px-6 py-4 text-xs font-bold text-gray-400 uppercase tracking-widest text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="results-table-body" class="divide-y divide-white/5">
                        <tr><td colspan="4" class="px-6 py-12 text-center text-gray-600 font-mono text-sm">Awaiting data stream...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const startButton = document.getElementById('start-scan');
        const buttonText = document.getElementById('button-text');
        const loadingSpinner = document.getElementById('loading-spinner');
        const resultsContainer = document.getElementById('results-container');
        const statusEl = document.getElementById('status');
        const resultsTableBody = document.getElementById('results-table-body');
        const downloadButton = document.getElementById('download-results');
        
        let allArtists = []; 
        let artistsAlreadyFound = new Set(); 
        let isScanning = false;
        const TOTAL_PAGES_TO_SCAN = 100; 

        startButton.addEventListener('click', handleStartScan);
        downloadButton.addEventListener('click', downloadResults);

        async function handleStartScan() {
            if (isScanning) return;
            isScanning = true;
            allArtists = [];
            artistsAlreadyFound.clear(); 
            
            resultsTableBody.innerHTML = `<tr><td colspan="4" class="px-6 py-12 text-center text-gray-500 font-mono animate-pulse">Scanning Global "New Release" Feed...</td></tr>`;
            resultsContainer.classList.remove('hidden');
            setTimeout(() => resultsContainer.classList.remove('opacity-0'), 100);
            
            downloadButton.classList.add('hidden');
            loadingSpinner.classList.remove('hidden');
            buttonText.textContent = "SCANNING...";
            startButton.disabled = true;
            startButton.classList.add('opacity-50', 'cursor-not-allowed');

            let totalArtistsFound = 0;

            try {
                resultsTableBody.innerHTML = ''; 
                for (let i = 0; i < TOTAL_PAGES_TO_SCAN; i++) {
                    const percent = Math.round(((i + 1) / TOTAL_PAGES_TO_SCAN) * 100);
                    updateStatus(`[PROCESSING] Batch ${i + 1}/${TOTAL_PAGES_TO_SCAN} | Found: ${totalArtistsFound}`);
                    
                    const response = await fetch('/api/scan_one_page', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ page_index: i, artists_already_found: Array.from(artistsAlreadyFound) })
                    });
                    
                    if (!response.ok) { console.error(`Page ${i} error`); continue; }

                    const data = await response.json();
                    const newArtists = data.artists || [];
                    
                    if (newArtists.length > 0) {
                        newArtists.forEach(artist => {
                            if (!artistsAlreadyFound.has(artist.id)) {
                                allArtists.push(artist);
                                artistsAlreadyFound.add(artist.id);
                                totalArtistsFound++;
                                renderRow(artist);
                            }
                        });
                        downloadButton.classList.remove('hidden');
                    }
                }
                updateStatus(`[COMPLETE] Scan Finalized. ${totalArtistsFound} targets secured.`);
                if (totalArtistsFound === 0) resultsTableBody.innerHTML = `<tr><td colspan="4" class="px-6 py-12 text-center text-gray-500 font-mono">No matching artists found in current batch.</td></tr>`;

            } catch (error) {
                showError(error.message);
            } finally {
                stopLoading();
            }
        }
        
        function stopLoading() {
            isScanning = false;
            loadingSpinner.classList.add('hidden');
            buttonText.textContent = "SCAN NEW RELEASES";
            startButton.disabled = false;
            startButton.classList.remove('opacity-50', 'cursor-not-allowed');
        }
        
        function renderRow(artist) {
            const row = document.createElement('tr');
            row.className = "group hover:bg-white/5 transition-colors border-b border-white/5 last:border-0";
            const followersFormatted = artist.followers.toLocaleString();
            
            const query = `"${artist.name}" (email OR contact OR site:facebook.com OR site:instagram.com)`;
            const searchUrl = `https://www.google.com/search?q=${encodeURIComponent(query)}`;
            
            row.innerHTML = `
                <td class="px-6 py-4 whitespace-nowrap">
                    <a href="${artist.url}" target="_blank" class="flex items-center space-x-3 group-hover:translate-x-1 transition-transform">
                         <div class="h-8 w-8 bg-zinc-800 rounded flex items-center justify-center text-xs font-bold text-gray-500 group-hover:bg-red-600 group-hover:text-white transition-colors">
                            ${artist.name.charAt(0)}
                        </div>
                        <span class="font-bold text-gray-200 group-hover:text-white">${artist.name}</span>
                    </a>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-gray-400 font-mono text-sm">${followersFormatted}</td>
                <td class="px-6 py-4 whitespace-nowrap">
                    <div class="flex items-center">
                        <span class="text-gray-400 font-mono text-sm mr-2">${artist.popularity}</span>
                        <div class="w-16 h-1 bg-zinc-800 rounded-full overflow-hidden">
                            <div class="h-full bg-red-600" style="width: ${artist.popularity}%"></div>
                        </div>
                    </div>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-right">
                    <a href="${searchUrl}" target="_blank" class="inline-flex items-center justify-center bg-transparent border border-gray-600 hover:border-red-500 hover:text-red-500 text-gray-400 text-xs font-bold py-2 px-4 rounded transition-all">
                        BROAD SEARCH 🔍
                    </a>
                </td>
            `;
            resultsTableBody.appendChild(row);
        }

        function downloadResults() {
            let text = "Artist Name\tFollowers\tPopularity\tSpotify URL\n"; 
            text += allArtists.map(a => `${a.name}\t${a.followers}\t${a.popularity}\t${a.url}`).join('\n');
            const blob = new Blob([text], { type: 'text/plain' });
            const anchor = document.createElement('a');
            anchor.download = 'scan_report.txt';
            anchor.href = window.URL.createObjectURL(blob);
            anchor.click();
            window.URL.revokeObjectURL(anchor.href);
        }

        function updateStatus(msg) { statusEl.textContent = msg; statusEl.classList.remove('text-red-500'); }
        function showError(msg) { statusEl.textContent = `[ERROR] ${msg}`; statusEl.classList.add('text-red-500'); stopLoading(); }
    </script>
</body>
</html>