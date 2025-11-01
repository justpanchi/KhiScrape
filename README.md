# KhiScrape (Khinsider Music Downloader)
Asynchronous Python script to download game soundtracks from [Khinsider](https://downloads.khinsider.com/).
Fetches all tracks from given album URLs, selects the best available format, and downloads them concurrently with rate limiting, retry handling, and automatic disc/track number extraction (with optional zero-padding, enabled by default).

## Configuration Options
- `urls`: Album URLs to download (required)
- `-o, --output PATH`: Output directory (default: KhiScrape)
- `-c, --concurrency NUM`: Concurrent downloads (default: 4)
- `-r, --rate-limit RPS`: Requests per second (default: 2.0)
- `-j, --jitter JITTER`: Jitter as percentage of base delay (default: 70%)
- `-f, --formats LIST`: Preferred formats (default: flac,wav,m4a,opus,ogg,aac,mp3)
- `-s, --chunk-size BYTES`: Chunk size, 0 for single write (default: 524288 / 512 KB)
- `-m, --max-retries NUM`: Retry attempts (default: 3)
- `-t, --track-padding {1,2,3,4}`: Track number padding (1=1,2,3; 2=01,02,03; 3=001,002,003; 4=0001,0002,0003). Default: auto-detect
- `-d, --debug`: Enable debug output (mess)

## Dependencies
- Python >=3.7
- Third-party: `pip install aiohttp aiofiles beautifulsoup4 colorama`

## Example Usage
```sh
python3 khiscrape.py https://downloads.khinsider.com/game-soundtracks/album/mario-kart-8-full-gamerip
```
