#!/usr/bin/python3
import sys
import argparse
import asyncio

# Define constants
STRIP_NAME = "LivingRoomStrip"
PLUG_NAME = "bedroom_notifier_plug"
WASHER_INTERVAL = "5"
DRYER_INTERVAL = "2"

async def run_script(script_name, *args):
    """Run an external script asynchronously with arguments."""
    process = await asyncio.create_subprocess_exec(
        "python3", script_name, *args
    )
    await process.wait()


def init_argparse() -> argparse.ArgumentParser:
    '''
    Initializes ArgumentParser for command line args when the script
    is used in that manner.

    Returns:
        argparse.ArgumentParser: initialized argparse
    '''
    parser = argparse.ArgumentParser(
        usage='%(prog)s [OPTIONS]',
        description='Notify washer or dryer done by blinking light(s)'
    )
    parser.add_argument('-d', '--dryer', action='store_true', help='Blink interval in seconds')
    return parser


async def main():
    parser = init_argparse()
    args = parser.parse_args()
    interval = DRYER_INTERVAL if args.dryer else WASHER_INTERVAL
    print(f"args.dryer: {args.dryer}, interval: {interval}")

    # First run strip_control.py
    await run_script("strip_control.py", STRIP_NAME, "on", "-b", "1", "-i", interval)

    # Then run plug_control.py
    await run_script("plug_control.py", PLUG_NAME, "on", "-b", "1", "-i", interval)

if __name__ == "__main__":
    asyncio.run(main())
