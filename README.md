# KhiScrape - Khinsider Music Downloader

Asynchronous Python script to download game soundtracks from [Khinsider](https://downloads.khinsider.com/). Fetches all tracks from given album URLs, selects the best available format from your format list, and downloads them concurrently with retry handling, rate limiting, and automatic disc/track number extraction (zero-padding enabled by default).

## ✨ Features

### 🎯 Core Functionality
- **Album Downloads** - Fetch all artworks and tracks from provided album URLs (supports IDs as input)
- **User Format Selection** - Automatically chooses best available audio format from your format list (tries each format in order of preference)
- **Multi-Disc Support** - Automatic disc detection
- **Track Padding** - Automatic track number extraction with configurable padding

### ⚡ Performance & Reliability
- **Asynchronous Downloads** - Concurrent downloads with configurable limits
- **Rate Limiting** - Adjustable rate limit in requests per second and jitter
- **Retry Mechanism** - Automatic retries with exponential backoff for failed downloads
- **Atomic Downloads** - Downloads are written to a temporary file and moved upon successful completion
- **File Verification** - Size validation to ensure complete downloads
- **Filesystem Safety** - Automatic path sanitization for cross-platform compatibility

### 🎛️ Configuration
- **Chunked Downloads** - Configurable chunk sizes for large files
- **Format Preferences** - Customize preferred audio formats (FLAC, MP3, etc.)
- **HTML Parser** - Configurable parser for BeautifulSoup
- **Track Number Padding** - Auto-detection or manual control over track numbering
- **Multi-Disc Padding Modes** - Padding per-disc or total track count

## 🚀 Quick Start

### Installation

KhiScrape can be installed either as a standalone script or as a packaged wheel. If you choose to install [lxml](https://github.com/lxml/lxml), building it requires libxml2 and libxslt to be installed.

#### Install as Script

**Install the dependencies:**
```sh
pip install aiohttp aiofiles beautifulsoup4 colorama yarl
```

**Install optional dependencies:**
```sh
pip install lxml
```

**Download the script file (curl/wget):**
```sh
curl -JO https://raw.githubusercontent.com/justpanchi/KhiScrape/refs/heads/main/khiscrape.py
```

```sh
wget --content-disposition https://raw.githubusercontent.com/justpanchi/KhiScrape/refs/heads/main/khiscrape.py
```

#### Install as Wheel

Requires [Git](https://git-scm.com/install/) to be installed.

**Without lxml:**
```sh
pip install "khiscrape @ git+https://github.com/justpanchi/KhiScrape#subdirectory=khiscrape_wheel"
```

**With lxml:**
```sh
pip install "khiscrape[lxml] @ git+https://github.com/justpanchi/KhiScrape#subdirectory=khiscrape_wheel"
```

### Usage

**Script installation:** Use `python3 khiscrape.py [OPTIONS] URLs/IDs...`
**Wheel installation:** Use `khiscrape [OPTIONS] URLs/IDs...`

#### Basic Examples

**Single album download:**
```sh
khiscrape https://downloads.khinsider.com/game-soundtracks/album/mario-kart-8-full-gamerip
```

**Multiple album downloads:**
```sh
khiscrape url1 url2 url3
```

#### Advanced Example

```sh
khiscrape \
  --output "$HOME/Music/Khinsider" \
  --concurrency 8 \
  --rate-limit 4 \
  --formats flac,mp3 \
  --padding-mode total \
  https://downloads.khinsider.com/game-soundtracks/album/pokemon-black-and-white-super-music-collection
```

## ⚙️ Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `urls` | Album URLs or IDs to download (required) | - |
| `-o, --output PATH` | Output directory | `KhiScrape` |
| `-a, --artworks-dir DIR` | Subdirectory for artworks (empty = no subdirectory) | `Artworks` |
| `-c, --concurrency NUM` | Concurrent downloads | `4` |
| `-r, --rate-limit RPS` | Requests per second | `2.0` |
| `-j, --jitter JITTER` | Jitter as percentage of base delay | `70%` |
| `-s, --chunk-size BYTES` | Chunk size (0 = single write) | `524288` (512KiB) |
| `-m, --max-retries NUM` | Retry attempts | `3` |
| `-f, --formats LIST` | Preferred formats in order | `flac,wav,m4a,opus,ogg,aac,mp3` |
| `-b, --html-parser PARSER` | HTML parser to use (`lxml`, `html.parser`, `html5lib`) | `lxml` (fallback: `html.parser`) |
| `-t, --track-padding NUM` | Track number padding (1-4 digits) | None (auto-detect) |
| `-p, --padding-mode MODE` | Multi-disc padding: `disc` or `total` | `disc` |
| `-d, --debug` | Enable debug output | `False` |

## 🛠️ Technical Details

### Requirements
- **Python**: >= 3.10
- **Dependencies**:
  - `aiohttp` - Asynchronous HTTP client
  - `aiofiles` - Async file operations
  - `beautifulsoup4` - HTML parsing
  - `colorama` - Cross-platform colored terminal output
  - `yarl` - URL handling (dependency of `aiohttp`)
- **Optional Dependencies:**
  - `lxml` - HTML parser

---

## License
MIT License

Copyright (c) 2025 PanChi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
