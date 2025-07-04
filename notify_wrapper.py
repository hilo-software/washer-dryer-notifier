import sys
import asyncio

# Define constants
STRIP_NAME = "LivingRoomStrip"
PLUG_NAME = "bedroom_notifier_plug"

async def run_script(script_name, *args):
    """Run an external script asynchronously with arguments."""
    process = await asyncio.create_subprocess_exec(
        "python3", script_name, *args
    )
    await process.wait()

async def main():
    # Run both scripts concurrently
    await asyncio.gather(
        run_script("strip_control.py", STRIP_NAME, "on", "-b", "1"),
        run_script("plug_control.py", PLUG_NAME, "on", "-b", "1"),
    )

if __name__ == "__main__":
    asyncio.run(main())
