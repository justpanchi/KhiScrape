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
from urllib.parse import urljoin, urlparse, unquote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from colorama import Fore, Style, init


init(autoreset=True)


try:
    import lxml  # noqa: F401

    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False


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
    chunk_size: Optional[int] = 512 * 1024  # 512 KiB
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
        "(KHTML, like Gecko) Chrome/143.0.7499.40 Safari/537.36"
    )
    base_referer: str = "https://downloads.khinsider.com/"
    debug: bool = False
    track_padding: Optional[int] = None  # None = auto-detect
    padding_mode: str = "disc"  # "disc" or "total"
    html_parser: str = field(
        default_factory=lambda: "lxml" if LXML_AVAILABLE else "html.parser"
    )

    def __post_init__(self) -> None:
        """Validate configuration values after initialization."""
        self._validate()

    def _validate(self) -> None:
        """Validate all configuration parameters."""
        if not isinstance(self.output_path, Path):
            raise ValueError(
                f"output_path must be a Path object, got {type(self.output_path)}"
            )

        if not isinstance(self.max_filename_bytes, int) or self.max_filename_bytes < 1:
            raise ValueError(
                f"max_filename_bytes must be a positive integer, got {self.max_filename_bytes}"
            )

        if not isinstance(self.preferred_formats, tuple):
            raise ValueError(
                f"preferred_formats must be a tuple, got {type(self.preferred_formats)}"
            )
        if not self.preferred_formats:
            raise ValueError("preferred_formats must not be empty")
        for fmt in self.preferred_formats:
            if not isinstance(fmt, str) or not fmt:
                raise ValueError(
                    f"All preferred_formats must be non-empty strings, got {self.preferred_formats}"
                )

        if not isinstance(self.max_concurrency, int) or self.max_concurrency < 1:
            raise ValueError(
                f"max_concurrency must be at least 1, got {self.max_concurrency}"
            )

        if self.chunk_size is not None:
            if not isinstance(self.chunk_size, int) or self.chunk_size <= 0:
                raise ValueError(
                    f"chunk_size must be positive integer or None, got {self.chunk_size}"
                )  # 0 → None in main()

        for timeout_name, timeout_value in [
            ("connection_timeout", self.connection_timeout),
            ("total_timeout", self.total_timeout),
            ("read_timeout", self.read_timeout),
        ]:
            if not isinstance(timeout_value, (int, float)) or timeout_value <= 0:
                raise ValueError(
                    f"{timeout_name} must be positive number, got {timeout_value}"
                )

        if not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError(
                f"max_retries must be non-negative integer, got {self.max_retries}"
            )

        if not isinstance(self.rate_limit, (int, float)) or self.rate_limit <= 0:
            raise ValueError(
                f"rate_limit must be positive number, got {self.rate_limit}"
            )

        if not isinstance(self.jitter_percent, (int, float)) or not (
            0 <= self.jitter_percent <= 100
        ):
            raise ValueError(
                f"jitter_percent must be between 0 and 100, got {self.jitter_percent}"
            )

        if (
            not isinstance(self.invalid_chars_pattern, str)
            or not self.invalid_chars_pattern
        ):
            raise ValueError(
                f"invalid_chars_pattern must be non-empty string, got {self.invalid_chars_pattern}"
            )

        if not isinstance(self.invalid_chars_replacement, str):
            raise ValueError(
                f"invalid_chars_replacement must be string, got {type(self.invalid_chars_replacement)}"
            )

        if not isinstance(self.user_agent, str) or not self.user_agent:
            raise ValueError(
                f"user_agent must be non-empty string, got {self.user_agent}"
            )

        if not isinstance(self.base_referer, str) or not self.base_referer:
            raise ValueError(
                f"base_referer must be non-empty string, got {self.base_referer}"
            )

        if self.track_padding is not None:
            if not isinstance(self.track_padding, int) or not (
                1 <= self.track_padding <= 4
            ):
                raise ValueError(
                    f"track_padding must be between 1 and 4 or None, got {self.track_padding}"
                )

        if not isinstance(self.padding_mode, str) or self.padding_mode not in (
            "disc",
            "total",
        ):
            raise ValueError(
                f"padding_mode must be 'disc' or 'total', got {self.padding_mode}"
            )

        if not isinstance(self.debug, bool):
            raise ValueError(f"debug must be boolean, got {type(self.debug)}")

        if not isinstance(self.html_parser, str):
            raise ValueError(
                f"html_parser must be string, got {type(self.html_parser)}"
            )

        valid_parsers = ["html.parser", "lxml", "html5lib"]
        if self.html_parser not in valid_parsers:
            raise ValueError(
                f"html_parser must be one of {valid_parsers}, got {self.html_parser}"
            )

        if self.html_parser == "lxml" and not LXML_AVAILABLE:
            raise ValueError(
                "lxml parser requested but lxml is not installed. Falling back to html.parser."
            )


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


@dataclass
class ArtworkInfo:
    """Information about a single artwork."""

    url: str
    filename: str
    file_size: int = 0


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

    # Only add handlers if none exist to avoid duplicates
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


class DownloadContext:
    """Context handling for tracks and artworks."""

    @staticmethod
    def get_track_context(track: TrackInfo) -> str:
        """Get track context for logging."""
        if track.disc_number is not None:
            return f"Track {track.disc_number}-{track.number:03d}"
        else:
            return f"Track {track.number:03d}"

    @staticmethod
    def get_artwork_context(artwork: ArtworkInfo) -> str:
        """Get artwork context for logging."""
        return f"Artwork {artwork.filename}"


class BaseDownloader:
    """Base downloader class with common download functionality."""

    def __init__(
        self, config: Config, logger: logging.Logger, rate_limiter: RateLimiter
    ) -> None:
        """Initialize the downloader."""
        self.config = config
        self.logger = logger
        self.rate_limiter = rate_limiter
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

    def _make_soup(self, html: str) -> BeautifulSoup:
        """Create BeautifulSoup object using configured parser."""
        return BeautifulSoup(html, self.config.html_parser)

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

    def _sanitize_filename(
        self,
        name: str,
        is_temp: bool = False,
    ) -> str:
        """Sanitize filename to be filesystem-safe."""
        # URL decode the filename first
        decoded_name = unquote(name)

        sanitized = re.sub(
            self.config.invalid_chars_pattern,
            self.config.invalid_chars_replacement,
            decoded_name,
        )

        max_bytes = self.config.max_filename_bytes
        if is_temp:
            max_bytes -= 1  # Reserve one byte for the dot prefix

        encoded_sanitized = sanitized.encode("utf-8")
        if len(encoded_sanitized) > max_bytes:
            truncated = encoded_sanitized[:max_bytes]
            # Avoid breaking UTF-8 sequences
            while truncated and truncated[-1] & 0x80 and not (truncated[-1] & 0x40):
                truncated = truncated[:-1]
            sanitized = truncated.decode("utf-8", errors="ignore")

        return sanitized

    async def _download_file(
        self,
        session: aiohttp.ClientSession,
        download_url: str,
        file_path: Path,
        referer: str,
        context: str = "File",
    ) -> bool:
        """Download a file with progress tracking and verification."""
        local_size = 0
        file_exists = file_path.exists()
        if file_exists:
            local_size = file_path.stat().st_size
            self.logger.debug(
                f"Local file exists with size: {local_size} bytes"
            )  # no context

        # For existing files, we need to check remote size first using HEAD
        remote_size = None
        if file_exists:
            remote_size = await self._get_remote_file_size(
                session, download_url, referer
            )
            if remote_size is not None:
                if local_size == remote_size:
                    self.logger.info(
                        f"{context} already exists with correct size: {file_path.name}"
                    )
                    return True
                else:
                    self.logger.warning(
                        f"{context} exists but size mismatch: local {local_size} vs remote {remote_size}. Redownloading."
                    )
            else:
                self.logger.warning(
                    f"Could not get remote {context.lower()} size, proceeding with download"
                )

        temp_path = file_path.parent / f".{file_path.name}"

        for attempt in range(self.config.max_retries + 1):
            try:
                await self.rate_limiter.acquire()

                async with session.get(
                    download_url,
                    headers={"Referer": referer},
                    timeout=self.timeout,
                ) as response:
                    response.raise_for_status()

                    # Get content length from GET response
                    content_length = int(response.headers.get("Content-Length", 0))

                    if not file_exists and content_length > 0:
                        remote_size = content_length
                        self.logger.debug(
                            f"Set file size from GET response: {content_length} bytes"
                        )  # no context

                    if file_exists and remote_size and content_length > 0:
                        if content_length != remote_size:
                            self.logger.warning(
                                f"Content-Length mismatch: HEAD {remote_size} vs GET {content_length}"
                            )  # no context

                    file_size_info = (
                        f" ({content_length / 1024 / 1024:.1f} MiB)"
                        if content_length > 0
                        else ""
                    )
                    self.logger.info(
                        f"Downloading {context.lower()}: {file_path.name}{file_size_info}"
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
                    if remote_size and remote_size > 0 and actual_size != remote_size:
                        raise ValueError(
                            f"Size mismatch: expected {remote_size}, got {actual_size}"
                        )

                    temp_path.rename(file_path)

                    if file_exists:
                        self.logger.info(
                            f"Successfully re-downloaded {context.lower()}: {file_path.name}"
                        )
                    else:
                        self.logger.info(
                            f"Successfully downloaded {context.lower()}: {file_path.name}"
                        )
                    return True

            except Exception as e:
                # Clean up temp file on error
                if temp_path.exists():
                    temp_path.unlink()

                if attempt < self.config.max_retries:
                    wait_time = 2**attempt
                    self.logger.warning(
                        f"{context} download failed (attempt {attempt + 1}/{self.config.max_retries + 1}): {e}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"{context} download failed after {self.config.max_retries} retries: {e}"
                    )
                    return False

        return False


class ArtworkDownloader(BaseDownloader):
    """Handles artwork downloading functionality."""

    async def _get_artwork_list(
        self, soup: BeautifulSoup, album_url: str
    ) -> List[ArtworkInfo]:
        """Extract artwork list from album page."""
        artworks = []

        try:
            # Find all album images in the page
            album_images = soup.find_all("div", class_="albumImage")

            for img_div in album_images:
                img_link = img_div.find("a", href=True)
                if img_link:
                    img_url = urljoin(album_url, img_link["href"])

                    # Skip thumbnail URLs
                    if "/thumbs/" in img_url:
                        self.logger.debug(f"Skipping thumbnail: {img_url}")
                        continue

                    filename = self._sanitize_filename(
                        Path(unquote(urlparse(img_url).path)).name
                    )

                    artwork = ArtworkInfo(url=img_url, filename=filename)
                    artworks.append(artwork)
                    self.logger.debug(f"Found artwork: {filename} -> {img_url}")

        except Exception as e:
            self.logger.warning(
                f"Error extracting artworks: {e}. Continuing without artworks..."
            )

        return artworks

    async def _download_artwork(
        self,
        session: aiohttp.ClientSession,
        artwork: ArtworkInfo,
        album_dir: Path,
        album_url: str,
    ) -> bool:
        """Download a single artwork."""
        async with self.semaphore:
            file_path = album_dir / artwork.filename
            return await self._download_file(
                session, artwork.url, file_path, album_url, context="Artwork"
            )

    async def download_artworks(
        self,
        session: aiohttp.ClientSession,
        artworks: List[ArtworkInfo],
        album_dir: Path,
        album_url: str,
    ) -> Tuple[int, int]:
        """Download all artworks for an album."""
        if not artworks:
            return 0, 0

        self.logger.info(f"Downloading {len(artworks)} artwork(s)...")

        download_tasks = []
        for artwork in artworks:
            task = self._download_artwork(session, artwork, album_dir, album_url)
            download_tasks.append(task)

        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        # Count successes
        successful = sum(1 for r in results if r is True)
        failed = len(artworks) - successful

        if successful > 0:
            self.logger.info(
                f"Artworks completed: {successful}/{len(artworks)} downloaded successfully"
            )
        if failed > 0:
            self.logger.warning(f"{failed} artwork(s) failed to download")

        return successful, failed


class TrackDownloader(BaseDownloader):
    """Handles track downloading functionality."""

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
            manual_padding = self.config.track_padding
            if self._is_multi_disc(tracks):
                disc_numbers = {
                    track.disc_number
                    for track in tracks
                    if track.disc_number is not None
                }
                return {disc: manual_padding for disc in disc_numbers}
            else:
                return {None: manual_padding}

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
            # Total padding: use total number of tracks across all discs
            total_tracks = len(tracks)
            if total_tracks < 10:
                padding = 1
            elif total_tracks < 100:
                padding = 2
            elif total_tracks < 1000:
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

    def _sanitize_track_filename(
        self,
        name: str,
        is_temp: bool = False,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
        padding: int = 2,
    ) -> str:
        """Sanitize track filename to be filesystem-safe."""
        sanitized_name = self._sanitize_filename(name, is_temp)

        if track_number is not None:
            formatted_track = self._format_track_number(track_number, padding)
            if disc_number is not None:
                prefix = f"{disc_number}-{formatted_track}. "
            else:
                prefix = f"{formatted_track}. "
        else:
            prefix = ""

        return prefix + sanitized_name

    async def _get_track_list(
        self, soup: BeautifulSoup, album_url: str
    ) -> List[TrackInfo]:
        """Extract track list from album page."""
        tracks = []
        tracklist_table = soup.find("table", id="songlist")

        if not tracklist_table:
            self.logger.error("Could not find tracklist table")  # Ooops!
            return tracks

        # Get all rows, skip header and footer
        rows = tracklist_table.find_all("tr")[1:]  # Skip header
        rows = [
            row for row in rows if row.get("id") != "songlist_footer"
        ]  # Skip footer

        if not rows:
            self.logger.error("No track rows found")  # Ooops!
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
            self.logger.error("Could not find track name column")  # Ooops!
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
        track_context = DownloadContext.get_track_context(track)

        response = await self._make_request(session, track.page_url, album_url)
        if not response:
            return False

        html = await response.text()
        soup = self._make_soup(html)

        download_links = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text().strip().lower()

            if any(
                f"download as {fmt}" in text for fmt in self.config.preferred_formats
            ):
                download_links.append((href, text))

        self.logger.debug(
            f"{track_context}: Found {len(download_links)} download links"
        )

        for fmt in self.config.preferred_formats:
            for href, text in download_links:
                if f"download as {fmt}" in text:
                    download_url = urljoin(track.page_url, href)
                    self.logger.debug(
                        f"{track_context}: Trying format {fmt}: {download_url}"
                    )

                    track.download_url = download_url
                    track.file_extension = f".{fmt}"
                    self.logger.debug(
                        f"{track_context}: Found download URL: {download_url}"
                    )
                    return True

        self.logger.warning(
            f"{track_context}: No preferred format found with accessible download"
        )
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
            track_context = DownloadContext.get_track_context(track)

            if not await self._get_download_info(session, track, album_url):
                self.logger.error(
                    f"{track_context}: Could not find download URL"
                )  # Ooops!
                return False

            track_padding = padding_dict.get(
                track.disc_number, 3
            )  # Default to 3 if not found
            sanitized_name = self._sanitize_track_filename(
                track.name,
                track_number=track.number,
                disc_number=track.disc_number,
                padding=track_padding,
            )
            file_path = album_dir / f"{sanitized_name}{track.file_extension}"

            return await self._download_file(
                session, track.download_url, file_path, track.page_url, context="Track"
            )

    async def download_tracks(
        self,
        session: aiohttp.ClientSession,
        soup: BeautifulSoup,
        album_dir: Path,
        album_url: str,
    ) -> Tuple[int, int]:
        """Download all tracks for an album."""
        tracks = await self._get_track_list(soup, album_url)
        if not tracks:
            self.logger.error("No tracks found in album")  # Ooops!
            return 0, 0

        padding_dict = self._calculate_track_padding(tracks)

        self._display_tracklist(tracks, padding_dict)

        self.logger.info(f"Downloading {len(tracks)} track(s)...")

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
            f"Tracks completed: {successful}/{len(tracks)} downloaded successfully"
        )
        if failed > 0:
            self.logger.warning(f"{failed} tracks failed to download")

        return successful, failed

    def _display_tracklist(
        self, tracks: List[TrackInfo], padding_dict: Dict[Optional[int], int]
    ) -> None:
        """Display the tracklist efficiently in a single call."""
        track_entries = []
        for track in tracks:
            track_padding = padding_dict.get(
                track.disc_number, 3
            )  # Default to 3 if not found
            formatted_track = self._format_track_number(track.number, track_padding)
            if track.disc_number is not None:
                track_entries.append(
                    f"{track.disc_number}-{formatted_track}. {track.name}"
                )
            else:
                track_entries.append(f"{formatted_track}. {track.name}")

        tracklist_text = "\n".join(track_entries)
        self.logger.info(
            f"Tracklist ({len(tracks)} tracks):", extra={"tracklist": True}
        )
        self.logger.info(tracklist_text, extra={"tracklist_content": True})


class KhinsiderDownloader:
    """Main downloader class for Khinsider albums."""

    def __init__(self, config: Config, logger: Optional[logging.Logger] = None) -> None:
        """Initialize the downloader."""
        self.config = config
        self.logger = logger or logging.getLogger("khinsider_downloader")
        self.rate_limiter = RateLimiter(config.rate_limit, config.jitter_percent)

        # Initialize specialized downloaders
        self.artwork_downloader = ArtworkDownloader(
            config, self.logger, self.rate_limiter
        )
        self.track_downloader = TrackDownloader(config, self.logger, self.rate_limiter)

    def _make_soup(self, html: str) -> BeautifulSoup:
        """Create BeautifulSoup object using configured parser."""
        return BeautifulSoup(html, self.config.html_parser)

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
                        return self.artwork_downloader._sanitize_filename(clean_text)

        # Fallback: use URL path
        fallback = urlparse(album_url).path.split("/")[-1] or "unknown_album"
        self.logger.warning(f"Using URL fallback for album name: {fallback}")
        return self.artwork_downloader._sanitize_filename(fallback)

    async def download_album(self, album_url: str) -> bool:
        """Download all artworks and tracks from an album."""
        self.logger.info(f"Processing album: {album_url}")

        connector = TCPConnector(limit=self.config.max_concurrency * 2)
        async with aiohttp.ClientSession(
            connector=connector, headers=self.artwork_downloader.headers
        ) as session:
            response = await self.artwork_downloader._make_request(
                session, album_url, self.config.base_referer
            )
            if not response:
                return False

            html = await response.text()
            soup = self._make_soup(html)

            album_name = await self._get_album_name(soup, album_url)
            album_dir = self.config.output_path / album_name
            album_dir.mkdir(parents=True, exist_ok=True)

            tracks = await self.track_downloader._get_track_list(soup, album_url)
            if not tracks:
                self.logger.error("No tracks found in album")  # Ooops!
                return False

            artworks = await self.artwork_downloader._get_artwork_list(soup, album_url)
            padding_dict = self.track_downloader._calculate_track_padding(tracks)

            self._display_album_info(
                album_name, album_dir, tracks, padding_dict, len(artworks)
            )

            # Download artworks
            artwork_success, artwork_failed = (
                await self.artwork_downloader.download_artworks(
                    session, artworks, album_dir, album_url
                )
            )

            # Download tracks
            track_success, track_failed = await self.track_downloader.download_tracks(
                session, soup, album_dir, album_url
            )

            total_success = artwork_success + track_success
            total_failed = artwork_failed + track_failed

            self.logger.info(
                f"Album completed: {track_success}/{len(tracks)} tracks, {artwork_success}/{len(artworks)} artworks downloaded successfully"
            )
            if total_failed > 0:
                self.logger.warning(f"{total_failed} items failed to download")

            return track_failed == 0  # Consider album success if all tracks downloaded

    def _display_album_info(
        self,
        album_name: str,
        album_dir: Path,
        tracks: List[TrackInfo],
        padding_dict: Dict[Optional[int], int],
        artwork_count: int,
    ) -> None:
        """Display album information and configuration."""
        is_multi_disc = self.track_downloader._is_multi_disc(tracks)
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
        lines.append(("Artworks", str(artwork_count), "key_value"))
        lines.append(
            (
                "Discs",
                f"{disc_count} {'(Multi-Disc)' if is_multi_disc else '(Single Disc)'}",
                "key_value",
            )
        )

        if self.config.track_padding is not None:
            lines.append(
                (
                    "Track Padding",
                    f"{self.config.track_padding} digit(s) (manual override)",
                    "key_value",
                )
            )
        elif is_multi_disc:
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

        if self.config.track_padding is None:
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
        lines.append(("HTML Parser", self.config.html_parser, "key_value"))
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
        "-b",
        "--html-parser",
        type=str,
        choices=["html.parser", "lxml", "html5lib"],
        help="HTML parser to use (default: lxml, fallback: html.parser)",
    )

    parser.add_argument(
        "-d", "--debug", action="store_true", help="Enable debug output"
    )

    args = parser.parse_args()

    # Set up logging for CLI usage
    logger = setup_logging(args.debug)

    try:
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
            html_parser=(
                args.html_parser
                if args.html_parser
                else ("lxml" if LXML_AVAILABLE else "html.parser")
            ),
            debug=args.debug,
        )
    except ValueError as e:
        print(f"{Fore.RED}Configuration error: {e}", file=sys.stderr)
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
