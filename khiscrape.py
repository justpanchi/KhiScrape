#!/usr/bin/env python3
"""Asynchronous Khinsider Music Downloader"""

import argparse
import asyncio
import logging
import random
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from urllib.parse import urljoin, urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from colorama import Fore, Style, init


init(autoreset=True)


@dataclass(frozen=True)
class Config:
    """Immutable configuration container."""

    output_path: Path = Path("KhiScrape")
    max_filename_bytes: int = 255
    preferred_formats: Tuple[str, ...] = (
        "flac",
        "wav",
        "m4a",
        "opus",
        "ogg",
        "aac",
        "mp3",
    )
    max_concurrency: int = 4
    chunk_size: Optional[int] = 512 * 1024  # 512 KB
    connection_timeout: float = 15.0
    total_timeout: float = 180.0
    read_timeout: float = 60.0
    max_retries: int = 3
    rate_limit: float = 2.0  # requests per second
    jitter_percent: float = 70.0  # Jitter as percentage of base delay
    invalid_chars_pattern: str = r'[\\/*?:"<>|]'
    invalid_chars_replacement: str = "_"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.7444.60 Safari/537.36"
    )
    base_referer: str = "https://downloads.khinsider.com/"
    debug: bool = False
    track_padding: Optional[int] = None  # None = auto-detect
    padding_mode: str = "disc"  # "disc" or "total"


@dataclass
class TrackInfo:
    """Information about a single track."""

    number: int
    name: str
    page_url: str
    disc_number: Optional[int] = None
    download_url: Optional[str] = None
    file_size: int = 0
    file_extension: str = ""


class ColorFormatter(logging.Formatter):
    """Custom formatter for colored logging output."""

    COLORS = {
        "INFO": Fore.CYAN,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "DEBUG": Fore.YELLOW,
        "TRACKLIST": Fore.MAGENTA,
        "SEPARATOR": Fore.GREEN,
        "HEADER": Fore.CYAN,
        "KEY_VALUE": Fore.WHITE,
        "VALUE": Fore.CYAN,
    }

    PREFIXES = {
        "INFO": "[INFO]",
        "WARNING": "[WARN]",
        "ERROR": "[ERROR]",
        "DEBUG": "[DEBUG]",
        "TRACKLIST": "[TRACKLIST]",
    }

    def format(self, record):
        if hasattr(record, "separator"):
            color = self.COLORS["SEPARATOR"]
            return f"{color}{record.getMessage()}{Style.RESET_ALL}"

        if hasattr(record, "header"):
            color = self.COLORS["HEADER"]
            return f"{color}{record.getMessage()}{Style.RESET_ALL}"

        if hasattr(record, "key_value"):
            key = getattr(record, "key", "")
            value = getattr(record, "value", "")
            key_color = self.COLORS["KEY_VALUE"]
            value_color = self.COLORS["VALUE"]
            return f"{key_color}{key}: {value_color}{value}{Style.RESET_ALL}"

        if hasattr(record, "tracklist_content"):
            return record.getMessage()

        if hasattr(record, "tracklist"):
            color = self.COLORS["TRACKLIST"]
            prefix = self.PREFIXES["TRACKLIST"]
        else:
            color = self.COLORS.get(record.levelname, "")
            prefix = self.PREFIXES.get(record.levelname, f"[{record.levelname}]")

        message = super().format(record)
        return f"{color}{prefix}{Style.RESET_ALL} {message}"


def setup_logging(
    debug: bool = False, logger: Optional[logging.Logger] = None
) -> logging.Logger:
    """Set up logging for the application."""
    if logger is None:
        logger = logging.getLogger("khinsider_downloader")

    # Only add handlers if none exist (to avoid duplicates in module usage)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG if debug else logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        console_handler.setFormatter(ColorFormatter())

        logger.addHandler(console_handler)

    return logger


class RateLimiter:
    """Global rate limiter for all HTTP requests with jitter support."""

    def __init__(self, rate: float, jitter_percent: float = 70.0) -> None:
        self.rate = rate
        self.base_delay = 1.0 / rate
        self.jitter_percent = jitter_percent
        self.max_delay = self.base_delay * (1 + jitter_percent / 100.0)
        self.last_request_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire permission to make a request, respecting rate limit with jitter."""
        async with self._lock:
            current_time = time.monotonic()
            time_since_last = current_time - self.last_request_time

            # Calculate required delay with jitter
            required_delay = random.uniform(self.base_delay, self.max_delay)

            # Always wait if we haven't reached the required delay
            if time_since_last < required_delay:
                await asyncio.sleep(required_delay - time_since_last)

            self.last_request_time = time.monotonic()


class KhinsiderDownloader:
    """Main downloader class for Khinsider albums."""

    def __init__(self, config: Config, logger: Optional[logging.Logger] = None) -> None:
        """Initialize the downloader."""
        self.config = config
        self.logger = logger or logging.getLogger("khinsider_downloader")
        self.rate_limiter = RateLimiter(config.rate_limit, config.jitter_percent)
        self.semaphore = asyncio.Semaphore(config.max_concurrency)

        self.timeout = ClientTimeout(
            connect=config.connection_timeout,
            total=config.total_timeout,
            sock_read=config.read_timeout,
        )

        self.headers = {
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        referer: Optional[str] = None,
        method: str = "GET",
    ) -> Optional[aiohttp.ClientResponse]:
        """Make an HTTP request with rate limiting and error handling."""
        await self.rate_limiter.acquire()

        headers = self.headers.copy()
        if referer:
            headers["Referer"] = referer

        for attempt in range(self.config.max_retries + 1):
            try:
                if method == "HEAD":
                    response = await session.head(
                        url, headers=headers, timeout=self.timeout
                    )
                else:
                    response = await session.get(
                        url, headers=headers, timeout=self.timeout
                    )

                response.raise_for_status()
                return response

            except Exception as e:
                if attempt < self.config.max_retries:
                    wait_time = 2**attempt
                    self.logger.warning(
                        f"Request failed (attempt {attempt + 1}/{self.config.max_retries + 1}): {e}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"Request failed after {self.config.max_retries} retries: {e}"
                    )
                    return None

    def _is_multi_disc(self, tracks: List[TrackInfo]) -> bool:
        """Check if album has multiple discs."""
        disc_numbers = {
            track.disc_number for track in tracks if track.disc_number is not None
        }
        return len(disc_numbers) > 1

    def _calculate_track_padding(
        self, tracks: List[TrackInfo]
    ) -> Dict[Optional[int], int]:
        """Calculate track number padding per disc based on padding mode and track data."""
        if self.config.track_padding is not None:
            # Manual padding overrides everything
            return {None: self.config.track_padding}

        is_multi_disc = self._is_multi_disc(tracks)

        if not is_multi_disc:
            # Single disc: use total track count
            total_tracks = len(tracks)
            if total_tracks < 10:
                return {None: 1}
            elif total_tracks < 100:
                return {None: 2}
            elif total_tracks < 1000:
                return {None: 3}
            else:
                return {None: 4}

        if self.config.padding_mode == "total":
            # Total padding: use maximum track number across all discs
            max_track_number = max(track.number for track in tracks)
            if max_track_number < 10:
                padding = 1
            elif max_track_number < 100:
                padding = 2
            elif max_track_number < 1000:
                padding = 3
            else:
                padding = 4
            disc_numbers = {
                track.disc_number for track in tracks if track.disc_number is not None
            }
            return {disc: padding for disc in disc_numbers}

        else:
            # Per-disc padding: calculate padding separately for each disc
            disc_tracks: Dict[Optional[int], List[TrackInfo]] = {}
            for track in tracks:
                disc_num = track.disc_number
                if disc_num not in disc_tracks:
                    disc_tracks[disc_num] = []
                disc_tracks[disc_num].append(track)

            padding_dict = {}
            for disc_num, disc_track_list in disc_tracks.items():
                max_track_in_disc = max(track.number for track in disc_track_list)
                if max_track_in_disc < 10:
                    padding_dict[disc_num] = 1
                elif max_track_in_disc < 100:
                    padding_dict[disc_num] = 2
                elif max_track_in_disc < 1000:
                    padding_dict[disc_num] = 3
                else:
                    padding_dict[disc_num] = 4

            return padding_dict

    def _format_track_number(self, track_number: int, padding: int) -> str:
        """Format track number with proper padding."""
        return f"{track_number:0{padding}d}"

    def _sanitize_filename(
        self,
        name: str,
        is_temp: bool = False,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
        padding: int = 2,
    ) -> str:
        """Sanitize filename to be filesystem-safe."""
        sanitized = re.sub(
            self.config.invalid_chars_pattern,
            self.config.invalid_chars_replacement,
            name,
        )

        if track_number is not None:
            formatted_track = self._format_track_number(track_number, padding)
            if disc_number is not None:
                prefix = f"{disc_number}-{formatted_track}. "
            else:
                prefix = f"{formatted_track}. "
        else:
            prefix = ""

        max_bytes = self.config.max_filename_bytes
        if is_temp:
            max_bytes -= 1  # Reserve one byte for the dot prefix

        prefix_bytes = len(prefix.encode("utf-8"))
        available_bytes = max_bytes - prefix_bytes

        encoded_sanitized = sanitized.encode("utf-8")
        if len(encoded_sanitized) > available_bytes:
            truncated = encoded_sanitized[:available_bytes]
            # Avoid breaking UTF-8 sequences
            while truncated and truncated[-1] & 0x80 and not (truncated[-1] & 0x40):
                truncated = truncated[:-1]
            sanitized = truncated.decode("utf-8", errors="ignore")

        return prefix + sanitized

    def _get_track_context(self, track: TrackInfo) -> str:
        """Get formatted track context for logging."""
        if track.disc_number is not None:
            return f"Disc {track.disc_number} Track {track.number:03d}: {track.name}"
        else:
            return f"Track {track.number:03d}: {track.name}"

    async def _get_album_name(self, soup: BeautifulSoup, album_url: str) -> str:
        """Extract album name from HTML with fallbacks."""
        # Try multiple strategies to extract album name
        strategies = [
            # Strategy 1: h2 tag in pageContent
            lambda: soup.select_one("#pageContent h2"),
            # Strategy 2: title tag
            lambda: soup.find("title"),
            # Strategy 3: meta description
            lambda: soup.find("meta", {"name": "description"}),
        ]

        for strategy in strategies:
            element = strategy()
            if element:
                text = (
                    element.get_text().strip()
                    if hasattr(element, "get_text")
                    else element.get("content", "")
                )
                if text:
                    clean_text = re.sub(r"\s+", " ", text)
                    clean_text = re.sub(r"MP3 - Download.*", "", clean_text)
                    clean_text = clean_text.strip()
                    if clean_text:
                        self.logger.debug(
                            f"Extracted album name via {strategy.__name__}: {clean_text}"
                        )
                        return self._sanitize_filename(clean_text)

        # Fallback: use URL path
        fallback = urlparse(album_url).path.split("/")[-1] or "unknown_album"
        self.logger.warning(f"Using URL fallback for album name: {fallback}")
        return self._sanitize_filename(fallback)

    async def _get_track_list(
        self, soup: BeautifulSoup, album_url: str
    ) -> List[TrackInfo]:
        """Extract track list from album page."""
        tracks = []
        tracklist_table = soup.find("table", id="songlist")

        if not tracklist_table:
            self.logger.error("Could not find tracklist table")
            return tracks

        # Get all rows, skip header and footer
        rows = tracklist_table.find_all("tr")[1:]  # Skip header
        rows = [
            row for row in rows if row.get("id") != "songlist_footer"
        ]  # Skip footer

        if not rows:
            self.logger.error("No track rows found")
            return tracks

        first_row_cells = rows[0].find_all("td")
        self.logger.debug(f"First row has {len(first_row_cells)} cells")

        has_disc_numbers = False
        has_track_numbers = False
        track_name_index = None

        for i, cell in enumerate(first_row_cells):
            if cell.get("class") and "clickable-row" in cell.get("class"):
                track_link = cell.find("a", href=True)
                if track_link:
                    cell_text = cell.get_text().strip()
                    is_duration = (
                        ":" in cell_text and any(c.isdigit() for c in cell_text)
                    ) or "MB" in cell_text
                    if not is_duration:
                        track_name_index = i
                        self.logger.debug(
                            f"Track name found at index {i}: '{cell_text}'"
                        )
                        break

        if track_name_index is None:
            self.logger.error("Could not find track name column")
            return tracks

        if track_name_index >= 3:
            # Structure: [play, disc, track_num, name, ...]
            has_disc_numbers = True
            has_track_numbers = True
            self.logger.debug("Detected structure: with disc numbers and track numbers")
        elif track_name_index == 2:
            prev_cell = first_row_cells[1]
            prev_text = prev_cell.get_text().strip().rstrip(".")
            if prev_text and (
                prev_text.isdigit()
                or (prev_text[:-1].isdigit() and prev_text.endswith("."))
            ):
                has_track_numbers = True
                self.logger.debug(
                    "Detected structure: with track numbers, no disc numbers"
                )
            else:
                self.logger.debug("Detected structure: no disc or track numbers")
        elif track_name_index == 1:
            self.logger.debug(
                "Detected structure: no disc or track numbers (minimal columns)"
            )

        current_track_number = 1

        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue

            disc_number = None
            track_number = current_track_number
            track_name = None
            track_url = None

            try:
                if has_disc_numbers and len(cells) > 3:
                    # Structure: [play, disc, track_num, name, ...]
                    disc_cell = cells[1]
                    track_number_cell = cells[2]
                    track_name_cell = cells[3]

                    disc_text = disc_cell.get_text().strip()
                    if disc_text and disc_text.isdigit():
                        disc_number = int(disc_text)

                    track_number_text = track_number_cell.get_text().strip().rstrip(".")
                    if track_number_text and track_number_text.isdigit():
                        track_number = int(track_number_text)

                elif has_track_numbers and len(cells) > 2:
                    # Structure: [play, track_num, name, ...]
                    track_number_cell = cells[1]
                    track_name_cell = cells[2]

                    track_number_text = track_number_cell.get_text().strip().rstrip(".")
                    if track_number_text and track_number_text.isdigit():
                        track_number = int(track_number_text)

                elif len(cells) > 1:
                    # Structure: [play, name, ...] - no disc or track numbers
                    for cell in cells[1:]:
                        if cell.get("class") and "clickable-row" in cell.get("class"):
                            track_link = cell.find("a", href=True)
                            if track_link:
                                cell_text = cell.get_text().strip()
                                is_duration = (
                                    ":" in cell_text
                                    and any(c.isdigit() for c in cell_text)
                                ) or "MB" in cell_text
                                if not is_duration:
                                    track_name_cell = cell
                                    break
                    else:
                        track_name_cell = cells[1] if len(cells) > 1 else None

                if track_name_cell:
                    track_link = track_name_cell.find("a", href=True)
                    if track_link:
                        track_name = track_link.get_text().strip()
                        track_url = urljoin(album_url, track_link["href"])

                if track_name and track_url:
                    track = TrackInfo(
                        number=track_number,
                        name=track_name,
                        page_url=track_url,
                        disc_number=disc_number,
                    )
                    tracks.append(track)
                    self.logger.debug(f"Found track: {track_name} -> {track_url}")
                    current_track_number += 1

            except Exception as e:
                self.logger.error(f"Error parsing track row: {e}")
                continue

        return tracks

    async def _get_download_info(
        self, session: aiohttp.ClientSession, track: TrackInfo, album_url: str
    ) -> bool:
        """Get the best available download URL for a track."""
        track_context = self._get_track_context(track)

        response = await self._make_request(session, track.page_url, album_url)
        if not response:
            return False

        html = await response.text()
        soup = BeautifulSoup(html, "html.parser")

        download_links = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text().strip().lower()

            if any(
                f"download as {fmt}" in text for fmt in self.config.preferred_formats
            ):
                download_links.append((href, text))

        self.logger.debug(f"Found {len(download_links)} download links", track_context)

        for fmt in self.config.preferred_formats:
            for href, text in download_links:
                if f"download as {fmt}" in text:
                    download_url = urljoin(track.page_url, href)
                    self.logger.debug(
                        f"Trying format {fmt}: {download_url}", track_context
                    )

                    track.download_url = download_url
                    track.file_extension = f".{fmt}"
                    self.logger.debug(
                        f"Found download URL: {download_url}", track_context
                    )
                    return True

        self.logger.warning(
            f"No preferred format found with accessible download", track_context
        )
        return False

    async def _get_remote_file_size(
        self, session: aiohttp.ClientSession, download_url: str, referer: str
    ) -> Optional[int]:
        """Get file size from remote server using HEAD request."""
        response = await self._make_request(
            session, download_url, referer, method="HEAD"
        )
        if response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    return int(content_length)
                except ValueError:
                    self.logger.debug(f"Invalid Content-Length: {content_length}")
        return None

    async def _should_skip_download(self, file_path: Path, expected_size: int) -> bool:
        """Check if file already exists with correct size."""
        if not file_path.exists():
            return False

        actual_size = file_path.stat().st_size
        if actual_size == expected_size:
            return True

        self.logger.debug(
            f"File size mismatch: local {actual_size} vs remote {expected_size}"
        )
        return False

    async def _download_file(
        self, session: aiohttp.ClientSession, track: TrackInfo, file_path: Path
    ) -> bool:
        """Download a file with progress tracking and verification."""
        # Check if file exists and get local size for comparison
        local_size = 0
        file_exists = file_path.exists()
        if file_exists:
            local_size = file_path.stat().st_size
            self.logger.debug(f"Local file exists with size: {local_size} bytes")

        # For existing files, we need to check remote size first using HEAD
        if file_exists:
            remote_size = await self._get_remote_file_size(
                session, track.download_url, track.page_url
            )
            if remote_size is not None:
                if local_size == remote_size:
                    self.logger.info(
                        f"File already exists with correct size: {file_path.name}"
                    )
                    return True
                else:
                    self.logger.warning(
                        f"File exists but size mismatch: local {local_size} vs remote {remote_size}. Redownloading."
                    )
                    track.file_size = remote_size
            else:
                self.logger.warning(
                    "Could not get remote file size, proceeding with download"
                )
                # If we can't get remote size, proceed with download but don't set expected size

        file_size_info = (
            f" ({track.file_size / 1024 / 1024:.1f} MB)" if track.file_size > 0 else ""
        )
        self.logger.info(f"Downloading: {file_path.name}{file_size_info}")

        temp_path = file_path.parent / f".{file_path.name}"

        for attempt in range(self.config.max_retries + 1):
            try:
                await self.rate_limiter.acquire()

                async with session.get(
                    track.download_url,
                    headers={"Referer": track.page_url},
                    timeout=self.timeout,
                ) as response:
                    response.raise_for_status()

                    # Get content length from GET response
                    content_length = int(response.headers.get("Content-Length", 0))

                    # For new files, set the file size from the GET response
                    if not file_exists and content_length > 0:
                        track.file_size = content_length
                        self.logger.debug(
                            f"Set file size from GET response: {content_length} bytes"
                        )

                    if file_exists and track.file_size > 0 and content_length > 0:
                        if content_length != track.file_size:
                            self.logger.warning(
                                f"Content-Length mismatch: HEAD {track.file_size} vs GET {content_length}"
                            )

                    async with aiofiles.open(temp_path, "wb") as f:
                        if self.config.chunk_size is None:
                            # Single write
                            content = await response.read()
                            await f.write(content)
                        else:
                            # Chunked write
                            total_downloaded = 0
                            async for chunk in response.content.iter_chunked(
                                self.config.chunk_size
                            ):
                                await f.write(chunk)
                                total_downloaded += len(chunk)

                    actual_size = temp_path.stat().st_size
                    if track.file_size > 0 and actual_size != track.file_size:
                        raise ValueError(
                            f"Size mismatch: expected {track.file_size}, got {actual_size}"
                        )

                    temp_path.rename(file_path)

                    if file_exists:
                        self.logger.info(
                            f"Successfully re-downloaded: {file_path.name}"
                        )
                    else:
                        self.logger.info(f"Successfully downloaded: {file_path.name}")
                    return True

            except Exception as e:
                # Clean up temp file on error
                if temp_path.exists():
                    temp_path.unlink()

                if attempt < self.config.max_retries:
                    wait_time = 2**attempt
                    self.logger.warning(
                        f"Download failed (attempt {attempt + 1}/{self.config.max_retries + 1}): {e}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"Download failed after {self.config.max_retries} retries: {e}"
                    )
                    return False

        return False

    async def _download_track(
        self,
        session: aiohttp.ClientSession,
        track: TrackInfo,
        album_dir: Path,
        album_url: str,
        padding_dict: Dict[Optional[int], int],
    ) -> bool:
        """Download a single track."""
        async with self.semaphore:
            track_context = self._get_track_context(track)
            self.logger.debug("Processing track", track_context)

            if not await self._get_download_info(session, track, album_url):
                self.logger.error("Could not find download URL", track_context)
                return False

            track_padding = padding_dict.get(
                track.disc_number, 3
            )  # Default to 3 if not found

            sanitized_name = self._sanitize_filename(
                track.name,
                track_number=track.number,
                disc_number=track.disc_number,
                padding=track_padding,
            )
            file_path = album_dir / f"{sanitized_name}{track.file_extension}"

            success = await self._download_file(session, track, file_path)

            if success:
                self.logger.debug("Completed", track_context)
            else:
                self.logger.error("Failed", track_context)

            return success

    async def download_album(self, album_url: str) -> bool:
        """Download all tracks from an album."""
        self.logger.info(f"Processing album: {album_url}")

        connector = TCPConnector(limit=self.config.max_concurrency * 2)
        async with aiohttp.ClientSession(
            connector=connector, headers=self.headers
        ) as session:
            response = await self._make_request(
                session, album_url, self.config.base_referer
            )
            if not response:
                return False

            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")

            album_name = await self._get_album_name(soup, album_url)
            album_dir = self.config.output_path / album_name
            album_dir.mkdir(parents=True, exist_ok=True)

            tracks = await self._get_track_list(soup, album_url)
            if not tracks:
                self.logger.error("No tracks found in album")
                return False

            padding_dict = self._calculate_track_padding(tracks)

            self._display_album_info(album_name, album_dir, tracks, padding_dict)
            self._display_tracklist(tracks)

            download_tasks = []
            for track in tracks:
                task = self._download_track(
                    session, track, album_dir, album_url, padding_dict
                )
                download_tasks.append(task)

            results = await asyncio.gather(*download_tasks, return_exceptions=True)

            # Count successes
            successful = sum(1 for r in results if r is True)
            failed = len(tracks) - successful

            self.logger.info(
                f"Album completed: {successful}/{len(tracks)} tracks downloaded successfully"
            )
            if failed > 0:
                self.logger.warning(f"{failed} tracks failed to download")

            return failed == 0

    def _display_album_info(
        self,
        album_name: str,
        album_dir: Path,
        tracks: List[TrackInfo],
        padding_dict: Dict[Optional[int], int],
    ) -> None:
        """Display album information and configuration."""
        is_multi_disc = self._is_multi_disc(tracks)
        disc_numbers = sorted(
            {track.disc_number for track in tracks if track.disc_number is not None}
        )
        disc_count = len(disc_numbers) or 1

        base_delay_ms = (1.0 / self.config.rate_limit) * 1000
        max_delay_ms = base_delay_ms * (1 + self.config.jitter_percent / 100.0)

        lines = []
        lines.append(("=" * 60, "separator"))
        lines.append(("ALBUM INFORMATION", "header"))
        lines.append(("=" * 60, "separator"))
        lines.append(("Album", album_name, "key_value"))
        lines.append(("Output", str(album_dir), "key_value"))
        lines.append(("Tracks", str(len(tracks)), "key_value"))
        lines.append(
            (
                "Discs",
                f"{disc_count} {'(Multi-Disc)' if is_multi_disc else '(Single Disc)'}",
                "key_value",
            )
        )

        if is_multi_disc:
            if self.config.padding_mode == "total":
                first_padding = next(iter(padding_dict.values()))
                lines.append(
                    (
                        "Track Padding",
                        f"{first_padding} digit(s) (consistent across all discs)",
                        "key_value",
                    )
                )
            else:  # disc mode
                padding_info = []
                for disc_num in disc_numbers:
                    padding = padding_dict.get(disc_num, 2)
                    padding_info.append(f"Disc {disc_num}: {padding} digit(s)")
                lines.append(("Track Padding", ", ".join(padding_info), "key_value"))
        else:
            # Single disc
            padding = padding_dict.get(None, 2)
            lines.append(("Track Padding", f"{padding} digit(s)", "key_value"))

        lines.append(("Padding Mode", self.config.padding_mode, "key_value"))
        lines.append(("-" * 60, "separator"))
        lines.append(("CONFIGURATION", "header"))
        lines.append(("-" * 60, "separator"))
        lines.append(
            ("Concurrent Downloads", str(self.config.max_concurrency), "key_value")
        )
        lines.append(("Rate Limit", f"{self.config.rate_limit} RPS", "key_value"))
        lines.append(("Jitter", f"{self.config.jitter_percent}%", "key_value"))
        lines.append(
            ("Request Delay", f"{base_delay_ms:.0f}-{max_delay_ms:.0f} ms", "key_value")
        )
        lines.append(
            ("Preferred Formats", ", ".join(self.config.preferred_formats), "key_value")
        )
        lines.append(("Max Retries", str(self.config.max_retries), "key_value"))
        lines.append(
            ("Chunk Size", str(self.config.chunk_size or "Single write"), "key_value")
        )
        lines.append(("=" * 60, "separator"))

        for line in lines:
            if len(line) == 2:
                text, line_type = line
                if line_type == "separator":
                    self.logger.info(text, extra={"separator": True})
                elif line_type == "header":
                    self.logger.info(text, extra={"header": True})
                else:
                    self.logger.info(text)
            else:  # key_value
                key, value, line_type = line
                self.logger.info(
                    "", extra={"key_value": True, "key": key, "value": value}
                )

    def _display_tracklist(self, tracks: List[TrackInfo]) -> None:
        """Display the tracklist efficiently in a single call."""
        track_entries = []
        for track in tracks:
            if track.disc_number is not None:
                track_entries.append(
                    f"{track.disc_number}-{track.number:03d}. {track.name}"
                )
            else:
                track_entries.append(f"{track.number:03d}. {track.name}")

        tracklist_text = "\n".join(track_entries)
        self.logger.info(
            f"Tracklist ({len(tracks)} tracks):", extra={"tracklist": True}
        )
        self.logger.info(tracklist_text, extra={"tracklist_content": True})


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Khinsider Music Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s https://downloads.khinsider.com/game-soundtracks/album/mario-kart-8-full-gamerip
  %(prog)s --concurrency 8 --rate-limit 4 url1 url2 url3
  %(prog)s --output "$HOME/Music" --formats flac,mp3 url1 url2
  %(prog)s --jitter 50 --rate-limit 2 url1  # 2 RPS with 50%% jitter (500-750ms delays)
        """,
    )

    parser.add_argument("urls", nargs="+", help="Album URLs to download")

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Config.output_path,
        help=f"Base output directory (default: {Config.output_path})",
    )

    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=Config.max_concurrency,
        help=f"Maximum concurrent downloads (default: {Config.max_concurrency})",
    )

    parser.add_argument(
        "-r",
        "--rate-limit",
        type=float,
        default=Config.rate_limit,
        help=f"Global rate limit in requests per second (default: {Config.rate_limit})",
    )

    parser.add_argument(
        "-j",
        "--jitter",
        type=float,
        default=Config.jitter_percent,
        help=f"Jitter as percentage of base delay (default: {Config.jitter_percent}%%)",
    )

    parser.add_argument(
        "-f",
        "--formats",
        type=lambda s: [f.strip().lower() for f in s.split(",")],
        default=Config.preferred_formats,
        help=f"Preferred formats in order (default: {','.join(Config.preferred_formats)})",
    )

    parser.add_argument(
        "-s",
        "--chunk-size",
        type=int,
        default=Config.chunk_size,
        help=f"Chunk size in bytes, 0 for single write (default: {Config.chunk_size})",
    )

    parser.add_argument(
        "-m",
        "--max-retries",
        type=int,
        default=Config.max_retries,
        help=f"Maximum retry attempts (default: {Config.max_retries})",
    )

    parser.add_argument(
        "-t",
        "--track-padding",
        type=int,
        choices=[1, 2, 3, 4],
        help="Track number padding (1=1,2,3; 2=01,02,03; 3=001,002,003; 4=0001,0002,0003). Default: auto-detect",
    )

    parser.add_argument(
        "-p",
        "--padding-mode",
        type=str,
        choices=["disc", "total"],
        default=Config.padding_mode,
        help="Padding mode for multi-disc albums: 'disc' (per-disc padding) or 'total' (total track count padding) (default: disc)",
    )

    parser.add_argument(
        "-d", "--debug", action="store_true", help="Enable debug output"
    )

    args = parser.parse_args()

    # Set up logging for CLI usage
    logger = setup_logging(args.debug)

    config = Config(
        output_path=args.output,
        max_concurrency=args.concurrency,
        rate_limit=args.rate_limit,
        jitter_percent=args.jitter,
        preferred_formats=tuple(args.formats),
        chunk_size=args.chunk_size if args.chunk_size != 0 else None,
        max_retries=args.max_retries,
        track_padding=args.track_padding,
        padding_mode=args.padding_mode,
        debug=args.debug,
    )

    if config.max_concurrency < 1:
        print(f"{Fore.RED}Error: Concurrency must be at least 1", file=sys.stderr)
        sys.exit(1)

    if config.rate_limit <= 0:
        print(f"{Fore.RED}Error: Rate limit must be positive", file=sys.stderr)
        sys.exit(1)

    if config.jitter_percent < 0 or config.jitter_percent > 100:
        print(f"{Fore.RED}Error: Jitter must be between 0 and 100", file=sys.stderr)
        sys.exit(1)

    # Pass the configured logger to the downloader
    downloader = KhinsiderDownloader(config, logger)

    successful_albums = 0
    for url in args.urls:
        try:
            success = await downloader.download_album(url)
            if success:
                successful_albums += 1
        except Exception as e:
            downloader.logger.error(f"Failed to process album {url}: {e}")

    summary_lines = []
    summary_lines.append(("=" * 60, "separator"))
    summary_lines.append(("SUMMARY", "header"))
    summary_lines.append(("=" * 60, "separator"))
    summary_lines.append(
        ("Albums processed", f"{successful_albums}/{len(args.urls)}", "key_value")
    )
    summary_lines.append(("=" * 60, "separator"))

    for line in summary_lines:
        if len(line) == 2:
            text, line_type = line
            if line_type == "separator":
                logger.info(text, extra={"separator": True})
            elif line_type == "header":
                logger.info(text, extra={"header": True})
            else:
                logger.info(text)
        else:  # key_value
            key, value, line_type = line
            logger.info("", extra={"key_value": True, "key": key, "value": value})


if __name__ == "__main__":
    asyncio.run(main())
