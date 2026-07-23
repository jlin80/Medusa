"""Punto de entrada: `python -m medusa` arranca el engine."""

import asyncio

from medusa.engine import main

if __name__ == "__main__":
    asyncio.run(main())
