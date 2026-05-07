#!/usr/bin/env python3
"""Standalone entry point for company discovery."""
import asyncio
import logging
import sys
from pathlib import Path

# Add root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.company_discovery import discover_companies

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    max_cos = 1000
    if len(sys.argv) > 1:
        try:
            max_cos = int(sys.argv[1])
        except ValueError:
            pass
            
    await discover_companies(max_companies=max_cos)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
