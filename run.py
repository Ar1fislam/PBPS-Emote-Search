import sys
import asyncio
import uvicorn

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if __name__ == "__main__":
    # Keep reload=False for Playwright stability on Windows
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
    # uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
