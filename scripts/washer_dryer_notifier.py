#!/usr/bin/python3

import asyncio
from kasa import Discover, SmartDevice
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, time
import logging
import argparse
from typing import Set, Union, ForwardRef, Dict, List, Optional
from os.path import isfile
from enum import Enum
import configparser
import traceback
from math import ceil
from dataclasses import dataclass
import atexit
import signal
import sys
import inspect
import requests
from logging.handlers import TimedRotatingFileHandler
from hilo_software_utilities.send_mail import send_text_email
from hilo_software_utilities.custom_logger import init_logging

APP_TAG = "washer dryer notifier"
LOG_FILE = "washer_dryer_notifier.log"
CONFIG_FILE = "washer_dryer_notifier.config"
SETUP_PROBE_INTERVAL_SECS = 30
RUNNING_TIME_WAIT_SECS = 60
RUNNING_SETUP_RETRY_MAX = 5
PROBE_INTERVAL_SECS = 5 * 60
PLUG_SETTLE_TIME_SECS = 10
RETRY_MAX = 3
RETRY_SLEEP_DELAY = 30
IDLE_TAG = 'idle'
RUNNING_TAG = 'running'
PUSHBULLET_CHANNEL_TAG = "washer_dryer_notifier"
INIT_TIMEOUT = 30
UPDATE_TIMEOUT = 10
TURN_ON_TIMEOUT = 10


class RunMode(Enum):
    SETUP = 0
    TEST = 1
    NORMAL = 2

class ApplianceType(Enum):
    WASHER = 0
    DRYER = 1


class ApplianceMode(Enum):
    IDLE = 1
    RUNNING = 2
    FINISHED = 3


class PushbulletBroadcaster:
    def __init__(self, access_token: str, channel_tag: str):
        self.access_token = access_token
        self.channel_tag = channel_tag
        self.headers = {
            "Access-Token": self.access_token,
            "Content-Type": "application/json"
        }

    
    def post_bullet(payload, headers: dict[str, str]) -> requests.Response:
        '''
        Wrapper for the request.post function

        Args:
            payload (_type_): http post payload
            headers (dict[str, str]): http header specific to Pushbullet with API key and channel_tag

        Returns:
            requests.Response: http response
        '''
        return requests.post("https://api.pushbullet.com/v2/pushes", json=payload, headers=headers)
    

    def send_notification(self, title: str, message: str):
        payload = {
            "type": "note",
            "title": title,
            "body": message,
            "channel_tag": self.channel_tag
        }

        response = PushbulletBroadcaster.post_bullet(payload, self.headers)

        if response.status_code == 200:
            logger.custom("✅ Notification sent successfully!")
        else:
            logger.error(f"❌ Failed to send notification: {response.status_code} - {response.json()}")



@dataclass
class AppliancePlugInfo():
    appliance_type: ApplianceType
    appliance_plug_name: str


    def __repr__(self):
        return f"AppliancePlugInfo(type={self.appliance_type}, name='{self.appliance_plug_name}')"


@dataclass
class AppliancePlug():
    appliance_plug_info: AppliancePlugInfo
    appliance_plug: SmartDevice


    def __repr__(self):
        return (f"AppliancePlug(info={repr(self.appliance_plug_info)}, "
                f"plug={self.appliance_plug})")


@dataclass
class EmailContext():
    email: str
    app_key: str


class Appliance():
    appliance_plug: AppliancePlug = None
    appliance_mode: ApplianceMode = ApplianceMode.IDLE
    appliance_idle_power: float = 0
    appliance_running_power: float = 0

    def __init__(self, plug: AppliancePlug):
        self.appliance_plug = plug
        self.appliance_mode = ApplianceMode.IDLE


    def __repr__(self):
        return (f"Appliance(name='{self.get_appliance_name()}', "
                f"mode={self.appliance_mode}, "
                f"idle={self.appliance_idle_power}, "
                f"running={self.appliance_running_power}, "
                f"plug={repr(self.appliance_plug)})")


    def get_appliance_name(self) -> str:
        return self.appliance_plug.appliance_plug_info.appliance_plug_name


    def get_appliance_mode(self) -> ApplianceMode:
        return self.appliance_mode
    

    def set_appliance_mode(self, mode: ApplianceMode) -> None:
        self.appliance_mode = mode
    
    
    def set_appliance_idle_power(self, appliance_idle_power: float) -> None:
        self.appliance_idle_power = appliance_idle_power


    def get_appliance_idle_power(self) -> float:
        return self.appliance_idle_power
    

    def set_appliance_running_power(self, appliance_running_power: float) -> None:
        self.appliance_running_power = appliance_running_power
    

    def get_appliance_running_power(self) -> float:
        return self.appliance_running_power

    async def query(self) -> ApplianceMode:
        '''
        State machine

        Returns:
            ApplianceMode: Resulting State
        '''
        logger.info(f"{self.get_appliance_name()}: query: ENTRY mode: {self.appliance_mode}")
        power = await self.get_power()
        match self.appliance_mode:
            case ApplianceMode.IDLE:
                if power <= (2 * self.appliance_idle_power):
                    pass
                else:
                    self.appliance_mode = ApplianceMode.RUNNING
            case ApplianceMode.RUNNING:
                if power == self.appliance_idle_power:
                    self.appliance_mode = ApplianceMode.FINISHED
        logger.info(f"{self.get_appliance_name()}: query: EXIT mode: {self.appliance_mode}")
        return self.appliance_mode
    

    async def get_power(self) -> float:
        await asyncio.wait_for(self.appliance_plug.appliance_plug.update(), UPDATE_TIMEOUT)
        return self.appliance_plug.appliance_plug.emeter_realtime.power


class ApplianceException(Exception):
    def __init__(self, msg: str):
        self.msg = msg


log_file = LOG_FILE
logger = None
washer: Appliance = None
dryer: Appliance = None
appliances: [] = []
setup_mode: bool = False
access_token: str = None
pbb: PushbulletBroadcaster = None
block_window: Optional[tuple] = None


def fn_name():
    return inspect.currentframe().f_back.f_code.co_name


def is_within_block(start: str, stop: str) -> bool:
    """Return True if current time falls within [start, stop)."""
    try:
        now = datetime.now().time()
        start_t = datetime.strptime(start, "%H:%M").time()
        stop_t = datetime.strptime(stop, "%H:%M").time()

        if start_t <= stop_t:
            return start_t <= now < stop_t
        else:
            # Handles overnight wrap (e.g. 22:00–06:00)
            return now >= start_t or now < stop_t
    except Exception as e:
        logger.error(f"is_within_block parse error: {e}")
        return False


async def init_plugs(target_plug_infos: list[AppliancePlugInfo]) -> list[AppliancePlug]:
    '''
    async function.  Uses kasa library to discover and find target device(s) matching target_plug(s) alias.

    Returns:
        list of matching plugs
    '''
    matching_plugs: list[AppliancePlug] = []
    try:
        found = await asyncio.wait_for(Discover.discover(), INIT_TIMEOUT)
        for smart_device in found.values():
            await asyncio.wait_for(smart_device.update(), UPDATE_TIMEOUT)
            for target_plug_info in target_plug_infos:
                if smart_device.alias == target_plug_info.appliance_plug_name:
                    if not smart_device.is_on:
                        if not await asyncio.wait_for(turn_on(smart_device), TURN_ON_TIMEOUT):
                            logger.warning(f"WARNING: Unable to turn on plug: {target_plug_info.appliance_plug_name}")
                            continue
                        logger.info(f"plug: was off, now successfully turned on so we delay {PLUG_SETTLE_TIME_SECS} seconds to allow power to settle")
                        await asyncio.sleep(PLUG_SETTLE_TIME_SECS)
                        await asyncio.wait_for(smart_device.update(), UPDATE_TIMEOUT)
                    matching_plugs.append(AppliancePlug(target_plug_info, smart_device))
    except TimeoutError as te:
        logger.error(f"init_plugs timed out: {te}")
    except Exception as e:
        logger.error(f"init_plugs Exception: {e}")
    return matching_plugs


async def turn_on(plug: SmartDevice) -> bool:
    try:
        await asyncio.wait_for(plug.turn_on(), TURN_ON_TIMEOUT)
        await asyncio.wait_for(plug.update(), UPDATE_TIMEOUT)
        return plug.is_on
    except TimeoutError as te:
        logger.error(f"turn_on timed out: {te}")
    except Exception as e:
        logger.error(f"turn_on Exception: {e}")
    return False


def get_power(plug: SmartDevice) -> float:
    return plug.emeter_realtime.power


async def notify_finished(appliance: Appliance, notifier_script: str = None, email_context = None, block_window = None) -> None:
    global pbb
    logger.custom(f"notify_finished: appliance: {appliance.get_appliance_name()}")

    # Suppress notification if within block window
    if block_window and is_within_block(*block_window):
        logger.custom(f"⏸ Notification suppressed (block window) for {appliance.get_appliance_name()}")
        return

    msg_status: str = " => FINISHED"
    msg_title: str = f"{APP_TAG}: {appliance.get_appliance_name()}"
    msg_string: str = f"{msg_title}{msg_status}"

    if pbb != None:
        pbb.send_notification(title=msg_title, message=msg_status)
    if email_context != None:
        send_text_email(email=email_context.email, app_key=email_context.app_key,
                        subject=APP_TAG, content=msg_string)
    if notifier_script is not None:
        process = await asyncio.create_subprocess_exec("python3", notifier_script)
        await process.wait()



def create_config_file(appliances: list[Appliance]) -> None:
    config = configparser.ConfigParser()
    for appliance in appliances:
        section_name = appliance.get_appliance_name()
        config.add_section(section_name)
        config.set(section_name, IDLE_TAG, str(appliance.get_appliance_idle_power()))
        config.set(section_name, RUNNING_TAG, str(appliance.get_appliance_running_power()))
    with open(CONFIG_FILE, "w") as config_file:
        config.write(config_file)


def read_config_file(appliances: list[Appliance]) -> Union[None, Exception]:
    config = configparser.ConfigParser()
    try:
        config.read(CONFIG_FILE)
        for appliance in appliances:
            section_name = appliance.get_appliance_name()
            appliance.set_appliance_idle_power = config[section_name][IDLE_TAG]
            appliance.set_appliance_running_power = config[section_name][RUNNING_TAG]
    except Exception as e:
        msg = f"Exception in read_config_file: {e}"
        logger.error(msg)
        raise Exception(msg)


async def setup_loop(appliances: list[Appliance]) -> bool:
    '''
    analyze app,iance idle and load power levels and create config file

    Args:
        appliances (list[Appliance]): _description_

    Returns:
        bool: _description_
    '''
    idle_power: float
    running_power: float
    idle_power_set: bool = True
    # Assume we start in idle mode and user manually turns on appliance(s) after 30s
    for appliance in appliances:
        idle_power = await appliance.get_power()
        appliance.set_appliance_idle_power(idle_power)
        logger.custom(f"We have set the IDLE power: {idle_power} for the appliance: {appliance.get_appliance_name()}")
    logger.custom(f"We have set the IDLE power for the appliance(s)")

    await asyncio.sleep(RUNNING_TIME_WAIT_SECS)

    running_power_set: bool = True
    retry_count = 0
    elapsed_seconds = 0
    retry_seconds_max = RUNNING_SETUP_RETRY_MAX * RUNNING_TIME_WAIT_SECS
    while True:
        for appliance in appliances:
            if appliance.appliance_running_power <= (2 * appliance.appliance_idle_power):
                running_power = await appliance.get_power()
                if running_power <= (2 * appliance.appliance_idle_power):
                    running_power_set = False
                else:
                    appliance.set_appliance_running_power(running_power)
        if running_power_set:
            logger.custom("Running power set for appliance(s)")
            break

        logger.warning(f"Failed to detect RUNNING power, retry {retry_count}")
        running_power_set = True
        retry_count += 1
        await asyncio.sleep(SETUP_PROBE_INTERVAL_SECS)
        elapsed_seconds += SETUP_PROBE_INTERVAL_SECS
        if elapsed_seconds > retry_seconds_max:
            logger.error(f"UNABLE to set running power in one or more appliances")
            break
    logger.custom(f"setup_loop: running_power_set: {running_power_set}, retry_count: {retry_count}, elapsed_seconds: {elapsed_seconds}")
    #  if successful, create a config file
    if idle_power_set and running_power_set:
        create_config_file(appliances)
    else:
        return False
    return True


async def verify_appliances(appliance_plug_infos: list[AppliancePlugInfo]) -> Union[list[Appliance], ApplianceException]:
    appliance_plugs: list[AppliancePlug] = await init_plugs(appliance_plug_infos)
    if len(appliance_plugs) != len(appliance_plug_infos):
        return []
    appliances: list[Appliance] = []
    for appliance_plug in appliance_plugs:
        appliances.append(Appliance(appliance_plug))
    return appliances


async def main_loop(run_mode: RunMode, plug_names: list[AppliancePlugInfo],
                    max_iterations: int = None, notifier_script: str = None,
                    email_context=None, block_window=None) -> bool:
    iterations = 0
    if len(plug_names) == 0:
        logger.error(f"No washer/dryer specified")
        return False
    appliances = await verify_appliances(plug_names)
    if len(appliances) == 0:
        logger.error(f"No appliances verified")
        return False
    logger.info(f"setup_mode: {setup_mode}, appliances: {repr(appliances)}")
    # Handle special run_modes
    if run_mode == RunMode.SETUP:
        return await setup_loop(appliances)
    if run_mode == RunMode.TEST:
        logger.warning(f"test_mode, sending notification")
        pbb.send_notification("TEST notification", "FUBAR")
        if notifier_script is not None:
            process = await asyncio.create_subprocess_exec("python3", notifier_script)
            await process.wait()
        return True

    try:
        # main running loop forever
        read_config_file(appliances)
        retry_ct = 0
        error_detected = False
        while retry_ct < RETRY_MAX:
            logger.info(f"main_loop: LOOP TOP")
            # Reset retry_ct if last pass through loop was successful
            if not error_detected:
                retry_ct = 0
            error_detected = False
            # Testability code
            if max_iterations is not None:
                iterations += 1
                if iterations >= max_iterations:
                    break
            try:
                for appliance in appliances:
                    appliance_state = await appliance.query()
                    if appliance_state == ApplianceMode.FINISHED:
                        await notify_finished(appliance, notifier_script,
                                              email_context=email_context,
                                              block_window=block_window)
                        appliance.set_appliance_mode(ApplianceMode.IDLE)
            except Exception as e:
                # Treat this as a network issue, retry after sleep up to RETRY_MAX attempts
                retry_ct = retry_ct + 1
                error_detected = True
                logger.error(f'Unexpected exception in main_loop: {e}, retry_ct: {retry_ct}')
                await asyncio.sleep(RETRY_SLEEP_DELAY)
            await asyncio.sleep(PROBE_INTERVAL_SECS)
        return True
    except Exception as e:
        logger.error(f"main_loop Exception: {e}")
    return False


def init_argparse() -> argparse.ArgumentParser:
    '''
    Initializes ArgumentParser for command line args when the script
    is used in that manner.

    Returns:
        argparse.ArgumentParser: initialized argparse
    '''
    parser = argparse.ArgumentParser(
        usage='%(prog)s [OPTIONS]',
        description='Notify when washer, dryer finishes'
    )
    parser.add_argument('-v', '--version', action='version',
                        version=f'%(prog)s version 1.0.0')
    parser.add_argument('-s', '--setup_mode', action='store_true',
                        help='setup mode, detect voltage levels and create config file')
    parser.add_argument('-t', '--test_mode', action='store_true',
                        help='test mode, send pushbullet broadcast test')
    parser.add_argument('-w', '--washer_plug_name', metavar='',
                        help='specifies washer plug name')
    parser.add_argument('-d', '--dryer_plug_name', metavar='',
                        help='specifies dryer plug name')
    parser.add_argument('-l', '--log_file_name', metavar='',
                        help='specifies custom log file name')
    parser.add_argument('-a', '--access_token', metavar='',
                        help='specifies pushbullet access token')
    parser.add_argument('-c', '--channel_tag', metavar='',
                        help='specifies pushbullet channel tag')
    parser.add_argument('-n', '--notifier_script', metavar='',
                        help='user defined script for custom notifications')
    parser.add_argument("-e", "--email", metavar='',
                        help='email address to send reports to')
    parser.add_argument('-k', '--app_key', metavar='',
                        help='Google app key for gmail reports')
    parser.add_argument(
        '-b', '--block_time', nargs=2, metavar=('START', 'STOP'),
        help='time window in 24h HH:MM HH:MM format to suppress notifications'
    )
    return parser

import contextlib

async def async_main(run_mode, plugs, notifier_script, email_context, block_window):
    """Wraps main_loop with graceful signal handling."""
    stop_event = asyncio.Event()

    def handle_stop_signal():
        logger.warning("🛑 Received stop signal (SIGTERM or SIGINT). Shutting down gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, handle_stop_signal)

    # Run your main loop in a background task so we can cancel it
    main_task = asyncio.create_task(
        main_loop(run_mode=run_mode, plug_names=plugs,
                  notifier_script=notifier_script,
                  email_context=email_context,
                  block_window=block_window)
    )

    # Wait until either the stop signal arrives or main_task finishes
    done, pending = await asyncio.wait(
        {main_task, asyncio.create_task(stop_event.wait())},
        return_when=asyncio.FIRST_COMPLETED
    )

    if stop_event.is_set():
        logger.info("Cancelling main task...")
        main_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await main_task

    logger.info("✅ Shutdown complete.")
    return True


def main() -> None:
    global log_file, logger, setup_mode, access_token, pbb, block_window

    plugs: list[AppliancePlugInfo] = []
    notifier_script: str = None
    run_mode: RunMode = RunMode.NORMAL
    email_context: EmailContext = None

    parser = init_argparse()
    args = parser.parse_args()

    # logging setup and argument parsing as before...
    if args.log_file_name != None:
        log_file = args.log_file_name
    if args.washer_plug_name:
        plugs.append(AppliancePlugInfo(ApplianceType.WASHER, args.washer_plug_name))
    if args.dryer_plug_name:
        plugs.append(AppliancePlugInfo(ApplianceType.DRYER, args.dryer_plug_name))
    if args.setup_mode:
        run_mode = RunMode.SETUP
    if args.access_token:
        access_token = args.access_token
    if args.channel_tag:
        channel_tag = args.channel_tag
    else:
        channel_tag = None
    if args.notifier_script:
        notifier_script = args.notifier_script
    if args.email and args.app_key:
        email_context = EmailContext(args.email, args.app_key)
    if args.test_mode:
        run_mode = RunMode.TEST
    if args.block_time:
        block_window = args.block_time

    logger = init_logging(log_file)

    if access_token is None or channel_tag is None:
        logger.warning("No access_token/channel_tag, cannot send pushbullet notifications")
    else:
        logger.info(f"pbb: access_token: {access_token}, channel_tag: {channel_tag}")
        pbb = PushbulletBroadcaster(access_token, channel_tag)
    
    logger.custom(f'>>>>> START washer_plug_name: {plugs}, run_mode: {run_mode}, pushbullet: {pbb}, block_window: {block_window} <<<<<')

    try:
        success = asyncio.run(async_main(run_mode, plugs, notifier_script, email_context, block_window))
    except Exception as e:
        logger.error(f"Exception in async_main: {e}")
        success = False

    logger.custom(f'>>>>> FINI <<<<< success: {success}')


if __name__ == '__main__':
    main()

