#!/usr/bin/env python3
"""Setup script for KhiScrape."""

from setuptools import setup

setup(
    name="khiscrape",
    version="0.2025.12.29.0",
    description="Asynchronous Khinsider Music Downloader",
    author="PanChi (justpanchi)",
    author_email="",
    url="https://github.com/justpanchi/KhiScrape",
    py_modules=["khiscrape"],
    entry_points={
        "console_scripts": [
            "khiscrape = khiscrape:main_sync",
        ],
    },
    install_requires=[
        "aiohttp",
        "aiofiles",
        "beautifulsoup4",
        "colorama",
        "yarl",
    ],
    extras_require={
        "lxml": ["lxml"],
    },
    python_requires=">=3.10",
    license="MIT",
    platforms=["any"],
)
