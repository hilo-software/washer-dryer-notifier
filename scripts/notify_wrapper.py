import sys
import asyncio

# Define the constant strip name
STRIP_NAME = "LivingRoomStrip"

async def main():
    
    # Call strip_control.py asynchronously
    process = await asyncio.create_subprocess_exec(
        "python3", "strip_control.py", STRIP_NAME, "on", "-b"
    )
    await process.wait()  # Wait for completion

if __name__ == "__main__":
    asyncio.run(main())
