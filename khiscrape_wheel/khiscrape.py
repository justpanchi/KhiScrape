#!/usr/bin/env python3
"""Asynchronous Khinsider Music Downloader"""

__version__ = "0.2026.01.16.0"

import argparse
import asyncio
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from colorama import Fore, Style, init
import yarl


init(autoreset=True)


try:
    import lxml  # noqa: F401

    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class Config:
    """Immutable configuration container."""

    output_path: Path = Path("KhiScrape")
    artworks_directory: str = "Artworks"  # Empty string = no subdirectory
    max_name_bytes: int = 255
    invalid_chars_pattern: str = r'[\\/*?:"<>|]'
    invalid_chars_replacement: str = "_"
    max_concurrency: int = 4
    rate_limit: float = 2.0  # requests per second
    jitter_percent: float = 70.0  # Jitter as percentage of base delay
    chunk_size: int | None = 512 * 1024  # 512 KiB
    max_retries: int = 3
    connection_timeout: float = 15.0
    read_timeout: float = 60.0
    total_timeout: float = 180.0
    html_parser: str = field(
        default_factory=lambda: "lxml" if LXML_AVAILABLE else "html.parser"
    )
    preferred_formats: tuple[str, ...] = field(
        default=("flac", "wav", "m4a", "opus", "ogg", "aac", "mp3")
    )
    track_padding: int | None = None  # None = auto-detect
    padding_mode: str = "disc"  # "disc" or "total"
    user_agent: str = field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.7499.40 Safari/537.36"
        )
    )
    base_album_url: str = "https://downloads.khinsider.com/game-soundtracks/album/"
    base_referer: str = "https://downloads.khinsider.com/"
    display_album_info: bool = True
    display_tracklist: bool = True
    debug: bool = False

    def __post_init__(self) -> None:
        """Validate configuration values after initialization."""
        self._validate()

    def _validate(self) -> None:
        """Validate all configuration parameters."""
        if not isinstance(self.output_path, Path):
            raise ValueError(
                f"output_path must be a Path object, got {type(self.output_path)}"
            )

        if not isinstance(self.artworks_directory, str):
            raise ValueError(
                f"artworks_directory must be a string, got {type(self.artworks_directory)}"
            )

        if not isinstance(self.max_name_bytes, int) or self.max_name_bytes < 32:
            raise ValueError(
                f"max_name_bytes must be at least 32, got {self.max_name_bytes}"
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

        if not isinstance(self.max_concurrency, int) or self.max_concurrency < 1:
            raise ValueError(
                f"max_concurrency must be at least 1, got {self.max_concurrency}"
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

        if self.chunk_size is not None:
            if not isinstance(self.chunk_size, int) or self.chunk_size <= 0:
                raise ValueError(
                    f"chunk_size must be positive integer or None, got {self.chunk_size}"
                )  # 0 → None in main()

        if not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError(
                f"max_retries must be non-negative integer, got {self.max_retries}"
            )

        for timeout_name, timeout_value in [
            ("connection_timeout", self.connection_timeout),
            ("read_timeout", self.read_timeout),
            ("total_timeout", self.total_timeout),
        ]:
            if not isinstance(timeout_value, (int, float)) or timeout_value <= 0:
                raise ValueError(
                    f"{timeout_name} must be positive number, got {timeout_value}"
                )

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

        if not isinstance(self.user_agent, str) or not self.user_agent:
            raise ValueError(
                f"user_agent must be non-empty string, got {self.user_agent}"
            )

        if not isinstance(self.base_album_url, str) or not self.base_album_url:
            raise ValueError(
                f"base_album_url must be non-empty string, got {self.base_album_url}"
            )

        if not isinstance(self.base_referer, str) or not self.base_referer:
            raise ValueError(
                f"base_referer must be non-empty string, got {self.base_referer}"
            )

        if not isinstance(self.display_album_info, bool):
            raise ValueError(
                f"display_album_info must be boolean, got {type(self.display_album_info)}"
            )

        if not isinstance(self.display_tracklist, bool):
            raise ValueError(
                f"display_tracklist must be boolean, got {type(self.display_tracklist)}"
            )

        if not isinstance(self.debug, bool):
            raise ValueError(f"debug must be boolean, got {type(self.debug)}")


class PathSanitizer:
    """Handles sanitization of paths and filenames for filesystem safety."""

    @staticmethod
    def _truncate_to_max_bytes(text: str, max_bytes: int) -> str:
        """Truncate string."""
        if max_bytes <= 0:
            return ""

        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text

        truncated = encoded[:max_bytes]
        # Avoid breaking UTF-8 sequences
        while truncated and truncated[-1] & 0x80 and not (truncated[-1] & 0x40):
            truncated = truncated[:-1]

        return truncated.decode("utf-8", errors="ignore")

    @staticmethod
    def sanitize_name(name: str, config: Config) -> str:
        """Sanitize content name."""
        return re.sub(
            config.invalid_chars_pattern,
            config.invalid_chars_replacement,
            name,
        )

    @staticmethod
    def limit_filename_length(
        name: str, config: Config, prefix: str, suffix: str, is_temp: bool = False
    ) -> str:
        """Apply length limiting to filename."""
        max_bytes = config.max_name_bytes
        if is_temp:
            max_bytes -= 1  # Reserve one byte for the dot prefix

        full_name = prefix + name + suffix
        encoded = full_name.encode("utf-8")

        if len(encoded) <= max_bytes:
            return full_name

        available_bytes = max_bytes - len((prefix + suffix).encode("utf-8"))
        truncated_name = PathSanitizer._truncate_to_max_bytes(name, available_bytes)
        return prefix + truncated_name + suffix

    @staticmethod
    def limit_directory_name(name: str, config: Config) -> str:
        """Apply length limiting to directory name."""
        return PathSanitizer._truncate_to_max_bytes(name, config.max_name_bytes)

    @staticmethod
    def sanitize_and_limit_filename(
        name: str,
        config: Config,
        prefix: str = "",
        suffix: str = "",
        is_temp: bool = False,
    ) -> str:
        """Sanitize and apply length limiting to filename."""
        sanitized = PathSanitizer.sanitize_name(name, config)
        return PathSanitizer.limit_filename_length(
            sanitized, config, prefix, suffix, is_temp
        )

    @staticmethod
    def sanitize_and_limit_directory(name: str, config: Config) -> str:
        """Sanitize and apply length limiting to directory name."""
        sanitized = PathSanitizer.sanitize_name(name, config)
        return PathSanitizer.limit_directory_name(sanitized, config)

    @staticmethod
    def sanitize_url_filename(url: str, config: Config) -> str:
        """Sanitize filename from URL."""
        url_obj = yarl.URL(url)
        filename = url_obj.parts[-1] if url_obj.parts else "unknown"
        return PathSanitizer.sanitize_name(filename, config)

    @staticmethod
    def sanitize_and_limit_path(path: Path, config: Config) -> Path:
        """Sanitize and limit each component of a path."""
        anchor = path.anchor  # Extracts root part (e.g., "/", "C:\", "\\server\share")

        components = path.parts[1:] if anchor else path.parts

        sanitized_components = [
            PathSanitizer.sanitize_and_limit_directory(comp, config)
            for comp in components
        ]

        return Path(anchor, *sanitized_components)


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


@dataclass(kw_only=True, slots=True)
class TrackInfo:
    """Information about a single track."""

    number: int
    name: str
    page_url: str
    disc_number: int | None = None
    download_url: str | None = None
    file_size: int = 0
    file_extension: str = ""


@dataclass(kw_only=True, slots=True)
class ArtworkInfo:
    """Information about a single artwork."""

    url: str
    filename: str
    file_size: int = 0


# -----------------------------------------------------------------------------
# Logging and Output
# -----------------------------------------------------------------------------


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
    logger: logging.Logger | None = None, debug: bool = False
) -> logging.Logger:
    """Set up logging for the application."""
    if logger is None:
        logger = logging.getLogger("khinsider_downloader")

    if not logger.handlers:
        logger.setLevel(logging.DEBUG if debug else logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        console_handler.setFormatter(ColorFormatter())

        logger.addHandler(console_handler)

    return logger


# -----------------------------------------------------------------------------
# Core Components
# -----------------------------------------------------------------------------


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

    @staticmethod
    def get_generic_context(item: TrackInfo | ArtworkInfo) -> str:
        """Get generic context name for logging."""
        return "Track" if isinstance(item, TrackInfo) else "Artwork"


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
            sock_read=config.read_timeout,
            total=config.total_timeout,
        )

        self.headers = {
            "User-Agent": config.user_agent,
            "Accept": "text/html;q=1.0,*/*;q=0.9",
            "Accept-Language": "en-US;q=1.0,en;q=0.9",
            "Accept-Encoding": "identity;q=1.0,gzip;q=0.9,deflate;q=0.8,br;q=0.7",
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
        referer: str | None = None,
        method: str = "GET",
    ) -> aiohttp.ClientResponse | None:
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

    def _get_specific_context(self, item: TrackInfo | ArtworkInfo) -> str:
        """Get specific context for logging."""
        if isinstance(item, TrackInfo):
            return DownloadContext.get_track_context(item)
        return DownloadContext.get_artwork_context(item)

    async def _get_remote_file_size(
        self,
        session: aiohttp.ClientSession,
        download_url: str,
        referer: str,
        item: TrackInfo | ArtworkInfo,
    ) -> int | None:
        """Get file size from remote server using HEAD request."""
        specific_context = self._get_specific_context(item)

        response = await self._make_request(
            session, download_url, referer, method="HEAD"
        )
        if response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    return int(content_length)
                except ValueError:
                    self.logger.debug(
                        f"{specific_context}: Invalid Content-Length: {content_length}"
                    )
        return None

    async def _download_file(
        self,
        session: aiohttp.ClientSession,
        download_url: str,
        file_path: Path,
        referer: str,
        item: TrackInfo | ArtworkInfo,
    ) -> bool:
        """Download a file with progress tracking and verification."""
        generic_context = DownloadContext.get_generic_context(item)
        specific_context = self._get_specific_context(item)

        local_size = 0
        file_exists = file_path.exists()
        if file_exists:
            local_size = file_path.stat().st_size
            self.logger.debug(
                f"{specific_context}: Local file exists with size: {local_size} bytes"
            )

        # For existing files, we need to check remote size first using HEAD
        remote_size = None
        if file_exists:
            remote_size = await self._get_remote_file_size(
                session, download_url, referer, item
            )
            if remote_size is not None:
                if local_size == remote_size:
                    self.logger.info(
                        f"{generic_context} already exists with correct size: {file_path.name}"
                    )
                    return True
                else:
                    self.logger.warning(
                        f"{specific_context}: exists but size mismatch: local {local_size} vs remote {remote_size}. Redownloading."
                    )
            else:
                self.logger.warning(
                    f"{specific_context}: Could not get remote size, proceeding with download"
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
                        self.logger.debug(
                            f"{specific_context}: Set file size from GET response: {content_length} bytes"
                        )

                    if file_exists and remote_size and content_length > 0:
                        if content_length != remote_size:
                            self.logger.warning(
                                f"{specific_context}: Content-Length mismatch: HEAD {remote_size} vs GET {content_length}"
                            )

                    file_size_info = (
                        f" ({content_length / 1024 / 1024:.1f} MiB)"
                        if content_length > 0
                        else ""
                    )
                    self.logger.info(
                        f"Downloading {generic_context.lower()}: {file_path.name}{file_size_info}"
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
                    if (
                        content_length
                        and content_length > 0
                        and actual_size != content_length
                    ):
                        raise ValueError(
                            f"Size mismatch: expected {content_length}, got {actual_size}"
                        )

                    temp_path.rename(file_path)

                    if file_exists:
                        self.logger.info(
                            f"Successfully re-downloaded {generic_context.lower()}: {file_path.name}"
                        )
                    else:
                        self.logger.info(
                            f"Successfully downloaded {generic_context.lower()}: {file_path.name}"
                        )
                    return True

            except Exception as e:
                # Clean up temp file on error
                if temp_path.exists():
                    temp_path.unlink()

                if attempt < self.config.max_retries:
                    wait_time = 2**attempt
                    self.logger.warning(
                        f"{specific_context}: download failed (attempt {attempt + 1}/{self.config.max_retries + 1}): {e}. Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"{specific_context}: download failed after {self.config.max_retries} retries: {e}"
                    )
                    return False

        return False


# -----------------------------------------------------------------------------
# Specialized Downloaders
# -----------------------------------------------------------------------------


class ArtworkDownloader(BaseDownloader):
    """Handles artwork downloading functionality."""

    async def _get_artwork_list(
        self, soup: BeautifulSoup, album_url: str
    ) -> list[ArtworkInfo]:
        """Extract artwork list from album page."""
        artworks = []

        try:
            # Find all album images in the page
            album_images = soup.find_all("div", class_="albumImage")

            for img_div in album_images:
                img_link = img_div.find("a", href=True)
                if img_link:
                    base_url = yarl.URL(album_url)
                    img_url = str(base_url.join(yarl.URL(img_link["href"])))

                    # Skip thumbnail URLs
                    if "/thumbs/" in img_url:
                        self.logger.debug(f"Skipping thumbnail: {img_url}")
                        continue

                    filename = PathSanitizer.sanitize_url_filename(img_url, self.config)

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
        album_url: str,
        artwork_dir: Path,
    ) -> bool:
        """Download a single artwork."""
        async with self.semaphore:
            name_without_ext = Path(artwork.filename).stem
            ext = Path(artwork.filename).suffix
            final_name = PathSanitizer.sanitize_and_limit_filename(
                name_without_ext, self.config, suffix=ext, is_temp=True
            )
            file_path = artwork_dir / final_name
            return await self._download_file(
                session, artwork.url, file_path, album_url, artwork
            )

    async def download_artworks(
        self,
        session: aiohttp.ClientSession,
        artworks: list[ArtworkInfo],
        album_url: str,
        album_dir: Path,
    ) -> tuple[int, int]:
        """Download all artworks for an album."""
        if not artworks:
            return 0, 0

        artwork_dir = album_dir
        if self.config.artworks_directory:
            sanitized_dir = PathSanitizer.sanitize_and_limit_directory(
                self.config.artworks_directory, self.config
            )
            artwork_dir = album_dir / sanitized_dir
            artwork_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"Downloading {len(artworks)} artwork(s)...")

        download_tasks = []
        for artwork in artworks:
            task = self._download_artwork(session, artwork, album_url, artwork_dir)
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

    def _is_multi_disc(self, tracks: list[TrackInfo]) -> bool:
        """Check if album has multiple discs."""
        disc_numbers = {
            track.disc_number for track in tracks if track.disc_number is not None
        }
        return len(disc_numbers) > 1

    def _calculate_track_padding(
        self, tracks: list[TrackInfo], is_multi_disc: bool
    ) -> dict[int | None, int]:
        """Calculate track number padding per disc based on padding mode and track data."""
        if self.config.track_padding is not None:
            # Manual padding overrides everything
            manual_padding = self.config.track_padding
            if is_multi_disc:
                disc_numbers = {
                    track.disc_number
                    for track in tracks
                    if track.disc_number is not None
                }
                return {disc: manual_padding for disc in disc_numbers}
            else:
                return {None: manual_padding}

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
            disc_tracks: dict[int | None, list[TrackInfo]] = {}
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
        track_number: int | None = None,
        disc_number: int | None = None,
        padding: int = 3,
        suffix: str = "",
    ) -> str:
        """Sanitize and apply length limiting to track filename."""
        sanitized_name = PathSanitizer.sanitize_name(name, self.config)

        if track_number is not None:
            formatted_track = self._format_track_number(track_number, padding)
            if disc_number is not None:
                prefix = f"{disc_number}-{formatted_track}. "
            else:
                prefix = f"{formatted_track}. "
        else:
            prefix = ""

        return PathSanitizer.limit_filename_length(
            sanitized_name, self.config, prefix, suffix, is_temp=True
        )

    def _display_tracklist(
        self, tracks: list[TrackInfo], padding_dict: dict[int | None, int]
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
        self.logger.info(f"({len(tracks)} tracks):", extra={"tracklist": True})
        self.logger.info(tracklist_text, extra={"tracklist_content": True})

    async def _get_track_list(
        self, soup: BeautifulSoup, album_url: str
    ) -> list[TrackInfo]:
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
                                if (
                                    is_duration := (
                                        ":" in cell_text
                                        and any(c.isdigit() for c in cell_text)
                                    )
                                    or "MB" in cell_text
                                ):
                                    continue
                                track_name_cell = cell
                                break
                    else:
                        track_name_cell = cells[1] if len(cells) > 1 else None

                if track_name_cell:
                    track_link = track_name_cell.find("a", href=True)
                    if track_link:
                        track_name = track_link.get_text().strip()
                        base_url = yarl.URL(album_url)
                        track_url = str(base_url.join(yarl.URL(track_link["href"])))

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
        specific_context = self._get_specific_context(track)

        response = await self._make_request(session, track.page_url, album_url)
        if not response:
            return False

        try:
            html = await response.text()
        except UnicodeDecodeError:
            html_bytes = await response.read()
            html = html_bytes.decode("utf-8", errors="replace")

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
            f"{specific_context}: Found {len(download_links)} download link(s)"
        )

        for fmt in self.config.preferred_formats:
            for href, text in download_links:
                if f"download as {fmt}" in text:
                    base_url = yarl.URL(track.page_url)
                    download_url = str(base_url.join(yarl.URL(href)))
                    self.logger.debug(
                        f"{specific_context}: Trying format {fmt}: {download_url}"
                    )

                    track.download_url = download_url
                    track.file_extension = f".{fmt}"
                    self.logger.debug(
                        f"{specific_context}: Found download URL: {download_url}"
                    )
                    return True

        self.logger.warning(
            f"{specific_context}: No preferred format found with accessible download"
        )
        return False

    async def _download_track(
        self,
        session: aiohttp.ClientSession,
        track: TrackInfo,
        album_url: str,
        album_dir: Path,
        padding_dict: dict[int | None, int],
    ) -> bool:
        """Download a single track."""
        async with self.semaphore:
            specific_context = self._get_specific_context(track)

            if not await self._get_download_info(session, track, album_url):
                self.logger.error(f"{specific_context}: Could not find download URL")
                return False

            track_padding = padding_dict.get(
                track.disc_number, 3
            )  # Default to 3 if not found
            sanitized_name = self._sanitize_track_filename(
                track.name,
                track_number=track.number,
                disc_number=track.disc_number,
                padding=track_padding,
                suffix=track.file_extension,
            )
            file_path = album_dir / sanitized_name

            return await self._download_file(
                session, track.download_url, file_path, track.page_url, track
            )

    async def download_tracks(
        self,
        session: aiohttp.ClientSession,
        tracks: list[TrackInfo],
        album_url: str,
        album_dir: Path,
        padding_dict: dict[int | None, int],
    ) -> tuple[int, int]:
        """Download all tracks for an album."""
        if not tracks:
            return 0, 0

        self.logger.info(f"Downloading {len(tracks)} track(s)...")

        download_tasks = []
        for track in tracks:
            task = self._download_track(
                session, track, album_url, album_dir, padding_dict
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


# -----------------------------------------------------------------------------
# Main Downloader
# -----------------------------------------------------------------------------


class KhinsiderDownloader:
    """Main downloader class for Khinsider albums."""

    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        """Initialize the downloader."""
        self.config = config
        self.logger = logger or logging.getLogger("khinsider_downloader")
        self.rate_limiter = RateLimiter(config.rate_limit, config.jitter_percent)

        # Initialize specialized downloaders
        self.artwork_downloader = ArtworkDownloader(
            config, self.logger, self.rate_limiter
        )
        self.track_downloader = TrackDownloader(config, self.logger, self.rate_limiter)

    def _normalize_album_url(self, input_str: str) -> str:
        """Normalize input to album URL."""
        # Strip query parameters and fragments
        clean_input = re.split(r"[?#]", input_str)[0]

        pattern = r"(?:/game-soundtracks)?/album/([^/?#]+)"
        match = re.search(pattern, clean_input)

        if match:
            album_id = match.group(1)
        else:
            album_id = clean_input.strip("/")

            if "/" in album_id:
                album_id = album_id.split("/")[-1]

        if not album_id:
            raise ValueError(f"Could not extract album ID from input: {input_str}")

        return str(yarl.URL(self.config.base_album_url) / album_id)

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
                        return PathSanitizer.sanitize_and_limit_directory(
                            clean_text, self.config
                        )

        # Fallback: use URL path
        url_obj = yarl.URL(album_url)
        fallback = url_obj.parts[-1] if url_obj.parts else "unknown_album"
        self.logger.warning(f"Using URL fallback for album name: {fallback}")
        return PathSanitizer.sanitize_and_limit_directory(fallback, self.config)

    def _display_album_info(
        self,
        album_name: str,
        tracks: list[TrackInfo],
        is_multi_disc: bool,
        disc_count: int,
        artwork_count: int,
        disc_numbers: list[int | None],
        album_dir: Path,
        padding_dict: dict[int | None, int],
    ) -> None:
        """Display album information and configuration."""
        base_delay_ms = (1.0 / self.config.rate_limit) * 1000
        max_delay_ms = base_delay_ms * (1 + self.config.jitter_percent / 100.0)

        lines = []
        lines.append(("=" * 60, "separator"))
        lines.append(("ALBUM INFORMATION", "header"))
        lines.append(("=" * 60, "separator"))
        lines.append(("Album", album_name, "key_value"))
        lines.append(("Output", str(album_dir), "key_value"))
        lines.append(
            (
                "Artworks Directory",
                (
                    self.config.artworks_directory
                    if self.config.artworks_directory
                    else "None (directly in album)"
                ),
                "key_value",
            )
        )
        lines.append(("Artworks", str(artwork_count), "key_value"))
        lines.append(("Tracks", str(len(tracks)), "key_value"))
        lines.append(
            (
                "Discs",
                f"{disc_count} {'(Multi-Disc)' if is_multi_disc else '(Single Disc)'}",
                "key_value",
            )
        )
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
            ("Chunk Size", str(self.config.chunk_size or "Single write"), "key_value")
        )
        lines.append(("Max Retries", str(self.config.max_retries), "key_value"))
        lines.append(("HTML Parser", self.config.html_parser, "key_value"))
        lines.append(
            ("Preferred Formats", ", ".join(self.config.preferred_formats), "key_value")
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

    async def download_album(self, album_input: str) -> bool:
        """Download all artworks and tracks from an album."""
        album_url = self._normalize_album_url(album_input)
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

            try:
                html = await response.text()
            except UnicodeDecodeError:
                html_bytes = await response.read()
                html = html_bytes.decode("utf-8", errors="replace")

            soup = self.track_downloader._make_soup(html)

            tracks = await self.track_downloader._get_track_list(soup, album_url)
            if not tracks:
                self.logger.error("No tracks found in album")
                return False

            artworks = await self.artwork_downloader._get_artwork_list(soup, album_url)

            sanitized_output_path = PathSanitizer.sanitize_and_limit_path(
                self.config.output_path, self.config
            )
            album_name = await self._get_album_name(soup, album_url)
            album_dir = sanitized_output_path / album_name
            album_dir.mkdir(parents=True, exist_ok=True)

            is_multi_disc = self.track_downloader._is_multi_disc(tracks)
            disc_numbers = sorted(
                {track.disc_number for track in tracks if track.disc_number is not None}
            )
            disc_count = len(disc_numbers) or 1

            padding_dict = self.track_downloader._calculate_track_padding(
                tracks, is_multi_disc
            )

            if self.config.display_album_info:
                self._display_album_info(
                    album_name,
                    tracks,
                    is_multi_disc,
                    disc_count,
                    len(artworks),
                    disc_numbers,
                    album_dir,
                    padding_dict,
                )
            else:
                self.logger.info(f"Output: {album_dir}")

            if self.config.display_tracklist:
                self.track_downloader._display_tracklist(tracks, padding_dict)

            # Download artworks
            artwork_success, artwork_failed = (
                await self.artwork_downloader.download_artworks(
                    session, artworks, album_url, album_dir
                )
            )

            # Download tracks
            track_success, track_failed = await self.track_downloader.download_tracks(
                session, tracks, album_url, album_dir, padding_dict
            )

            total_success = artwork_success + track_success
            total_failed = artwork_failed + track_failed

            self.logger.info(
                f"Album completed: {track_success}/{len(tracks)} tracks, {artwork_success}/{len(artworks)} artworks downloaded successfully"
            )
            if total_failed > 0:
                self.logger.warning(f"{total_failed} items failed to download")

            return track_failed == 0  # Consider album success if all tracks downloaded


# -----------------------------------------------------------------------------
# Command Line Interface
# -----------------------------------------------------------------------------


async def main() -> None:
    """Main entry point."""
    default_html_parser = Config.__dataclass_fields__["html_parser"].default_factory()
    default_preferred_formats = Config.__dataclass_fields__["preferred_formats"].default

    parser = argparse.ArgumentParser(
        description="Khinsider Music Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s https://downloads.khinsider.com/game-soundtracks/album/mario-kart-8-full-gamerip # URLs or IDs (mario-kart-8-full-gamerip)
  %(prog)s --output "$HOME/Music/Khinsider" --concurrency 8 --rate-limit 4 --formats flac,mp3 --padding-mode total url1 url2 url3
        """,
    )

    parser.add_argument("urls", nargs="+", help="Album URLs or IDs to download")

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Config.__dataclass_fields__["output_path"].default,
        help=f"Base output directory (default: {Config.__dataclass_fields__['output_path'].default})",
    )

    parser.add_argument(
        "-a",
        "--artworks-dir",
        type=str,
        default=Config.__dataclass_fields__["artworks_directory"].default,
        help=f"Subdirectory for artworks, empty for album directory (default: {Config.__dataclass_fields__['artworks_directory'].default})",
    )

    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=Config.__dataclass_fields__["max_concurrency"].default,
        help=f"Maximum concurrent downloads (default: {Config.__dataclass_fields__['max_concurrency'].default})",
    )

    parser.add_argument(
        "-r",
        "--rate-limit",
        type=float,
        default=Config.__dataclass_fields__["rate_limit"].default,
        help=f"Global rate limit in requests per second (default: {Config.__dataclass_fields__['rate_limit'].default})",
    )

    parser.add_argument(
        "-j",
        "--jitter",
        type=float,
        default=Config.__dataclass_fields__["jitter_percent"].default,
        help=f"Jitter as percentage of base delay (default: {Config.__dataclass_fields__['jitter_percent'].default}%%)",
    )

    parser.add_argument(
        "-s",
        "--chunk-size",
        type=int,
        default=Config.__dataclass_fields__["chunk_size"].default,
        help=f"Chunk size in bytes, 0 for single write (default: {Config.__dataclass_fields__['chunk_size'].default})",
    )

    parser.add_argument(
        "-m",
        "--max-retries",
        type=int,
        default=Config.__dataclass_fields__["max_retries"].default,
        help=f"Maximum retry attempts (default: {Config.__dataclass_fields__['max_retries'].default})",
    )

    parser.add_argument(
        "-b",
        "--html-parser",
        type=str,
        choices=["html.parser", "lxml", "html5lib"],
        help=f"HTML parser to use (default: {default_html_parser})",
    )

    parser.add_argument(
        "-f",
        "--formats",
        type=lambda s: [f.strip().lower() for f in s.split(",")],
        default=default_preferred_formats,
        help=f"Preferred formats in order (default: {','.join(default_preferred_formats)})",
    )

    parser.add_argument(
        "-t",
        "--track-padding",
        type=int,
        choices=[1, 2, 3, 4],
        help="Track number padding (1=1,2,3; 2=01,02,03; 3=001,002,003; 4=0001,0002,0003). Default: None (auto-detect)",
    )

    parser.add_argument(
        "-p",
        "--padding-mode",
        type=str,
        choices=["disc", "total"],
        default=Config.__dataclass_fields__["padding_mode"].default,
        help=f"Padding mode for multi-disc albums: 'disc' (per-disc padding) or 'total' (total track count padding) (default: {Config.__dataclass_fields__['padding_mode'].default})",
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    parser.add_argument(
        "-d", "--debug", action="store_true", help="Enable debug output"
    )

    args = parser.parse_args()

    # Set up logging for CLI usage
    logger = setup_logging(debug=args.debug)

    try:
        config = Config(
            output_path=args.output,
            artworks_directory=args.artworks_dir,
            max_concurrency=args.concurrency,
            rate_limit=args.rate_limit,
            jitter_percent=args.jitter,
            chunk_size=args.chunk_size if args.chunk_size != 0 else None,
            max_retries=args.max_retries,
            html_parser=args.html_parser if args.html_parser else default_html_parser,
            preferred_formats=tuple(args.formats),
            track_padding=args.track_padding,
            padding_mode=args.padding_mode,
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


def main_sync() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
