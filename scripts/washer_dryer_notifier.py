#!/usr/bin/python3

import asyncio
from kasa import Discover, SmartDevice
from datetime import datetime, timedelta
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
CUSTOM_LEVEL_NUM = 25
CUSTOM_LEVEL_NAME = "CUSTOM"
DEFAULT_LOGGING_LEVEL = CUSTOM_LEVEL_NUM
# DEFAULT_LOGGING_LEVEL = logging.INFO


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

        # response = requests.post("https://api.pushbullet.com/v2/pushes", json=payload, headers=self.headers)
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

def fn_name():
    return inspect.currentframe().f_back.f_code.co_name


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

async def notify_finished(appliance: Appliance, notifier_script: str = None) -> None:
    global pbb
    logger.custom(f"notify_finished: appliance: {appliance.get_appliance_name()}")

    if pbb == None:
        logger.error(f"notify_finished(), no pushbullet specified, will not notify channel")
    else:
        pbb.send_notification(f"{appliance.get_appliance_name()}", f"FINISHED")
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
        logger.error(f"Exception in read_config_file: {e}")
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
            logger.custom(f"We have set the RUNNING power for the appliance(s)")
            break

        logger.warning(f"At least one appliance failed to detect a valid RUNNING voltage, retry_count: {retry_count}")
        running_power_set = True
        retry_count += 1
        await asyncio.sleep(SETUP_PROBE_INTERVAL_SECS)
        elapsed_seconds += SETUP_PROBE_INTERVAL_SECS
        if elapsed_seconds > retry_seconds_max:
            logger.error(f"UNABLE to set running power in one or more appliances: {repr(appliances)}")
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


async def main_loop(run_mode: RunMode, plug_names: list[AppliancePlugInfo], max_iterations: int = None, notifier_script: str = None) -> bool:
    iterations = 0
    if len(plug_names) == 0:
        logger.error(f"ERROR, no washer or dryer specified, need at least one")
        return False
    appliances = await verify_appliances(plug_names)
    if len(appliances) == 0:
        logger.error(f"ERROR, no appliances verified")
        return False
    logger.info(f"setup_mode: {setup_mode}, appliances: {repr(appliances)}")
    # Handle special run_modes
    if run_mode == RunMode.SETUP:
        return await setup_loop(appliances)
    if run_mode == RunMode.TEST:
        logger.warning(f"main_loop: test_mode, sending notification")
        pbb.send_notification(f"TEST notification", "FUBAR")
        if notifier_script is not None:
            process = await asyncio.create_subprocess_exec("python3", notifier_script)
            await process.wait()
        return True

    try:
        # main running loop forever
        read_config_file(appliances)
        
        retry_ct = 0
        while retry_ct < RETRY_MAX:
            logger.info(f"main_loop: LOOP TOP")
            # Testability code
            if max_iterations is not None:
                iterations += 1
                if iterations >= max_iterations:
                    break
            try:
                for appliance in appliances:
                    appliance_state = await appliance.query()
                    if appliance_state == ApplianceMode.FINISHED:
                        appliance.set_appliance_mode(ApplianceMode.IDLE)
                        await notify_finished(appliance, notifier_script)
            except Exception as e:
                # Treat this as a network issue, retry after sleep up to RETRY_MAX attempts
                retry_ct = retry_ct + 1
                logger.error(f'ERROR, unexpected exit from main_loop: {e}, retry_ct: {retry_ct}')
                await asyncio.sleep(RETRY_SLEEP_DELAY)
            await asyncio.sleep(PROBE_INTERVAL_SECS)
        return True
    except Exception as e:
        logger.error(f"main_loop Exception: {e}")
    return False

def setup_logging_handlers(log_file: str) -> list:
    try:
        # Rotate logs every day (when='D') and keep the last 5 days (backupCount=5)
        logging_file_handler = TimedRotatingFileHandler(log_file, when='D', interval=1, backupCount=5)
    except (IOError, OSError, ValueError, FileNotFoundError) as e:
        print(f'ERROR -- Could not create logging file: {log_file}, e: {str(e)}')
        logging_handlers = [
            logging.StreamHandler()
        ]
        return logging_handlers
    except Exception as e:
        print(f'ERROR -- Unexpected Exception: Could not create logging file: {log_file}, e: {str(e)}')
        logging_handlers = [
            logging.StreamHandler()
        ]
        return logging_handlers

    logging_handlers = [
        logging_file_handler,
        logging.StreamHandler()
    ]
    return logging_handlers

# Define formats
default_format = "%(asctime)s %(levelname)s: %(message)s"
info_format = "%(message)s"
custom_format = "%(asctime)s CUSTOM: %(message)s"

def custom(self, message, *args, **kws):
    if self.isEnabledFor(CUSTOM_LEVEL_NUM):
        self._log(CUSTOM_LEVEL_NUM, message, args, **kws)

class CustomFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, info_fmt=None, custom_fmt=None, *args, **kwargs):
        super().__init__(fmt, datefmt, *args, **kwargs)
        self.default_fmt = fmt
        self.info_fmt = info_fmt
        self.custom_fmt = custom_fmt

    def format(self, record):
        # Use different format for INFO level
        if record.levelno == logging.INFO:
            self._style._fmt = self.info_fmt
        # Use different format for CUSTOM level
        elif record.levelno == CUSTOM_LEVEL_NUM:
            self._style._fmt = self.custom_fmt
            record.levelname = CUSTOM_LEVEL_NAME  # Ensure the custom level name is used
        else:
            self._style._fmt = self.default_fmt
        return super().format(record)

def init_logging(log_file: str) -> logging.Logger:
    logging.addLevelName(CUSTOM_LEVEL_NUM, CUSTOM_LEVEL_NAME)
    logging.Logger.custom = custom
    logger = logging.getLogger('')
    logger.setLevel(DEFAULT_LOGGING_LEVEL)
    formatter = CustomFormatter(fmt=default_format, info_fmt=info_format, custom_fmt=custom_format, datefmt="%Y-%m-%d %H:%M:%S")
    logging_handlers = setup_logging_handlers(log_file)
    for handler in logging_handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

# def setup_logging_handlers(log_file: str) -> list:
#     try:
#         logging_file_handler = logging.FileHandler(filename=log_file, mode='w')
#     except (IOError, OSError, ValueError, FileNotFoundError) as e:
#         print(f'ERROR -- Could not create logging file: {log_file}, e: {str(e)}')
#         logging_handlers = [
#             logging.StreamHandler()
#         ]
#         return logging_handlers
#     except Exception as e:
#         print(f'ERROR -- Unexpected Exception: Could not create logging file: {log_file}, e: {str(e)}')
#         logging_handlers = [
#             logging.StreamHandler()
#         ]
#         return logging_handlers

#     logging_handlers = [
#         logging_file_handler,
#         logging.StreamHandler()
#     ]
#     return logging_handlers

# # Define formats
# default_format = "%(asctime)s %(levelname)s: %(message)s"
# info_format = "%(message)s"
# custom_format = "%(asctime)s CUSTOM: %(message)s"


# def custom(self, message, *args, **kws):
#     if self.isEnabledFor(CUSTOM_LEVEL_NUM):
#         self._log(CUSTOM_LEVEL_NUM, message, args, **kws)


# class CustomFormatter(logging.Formatter):
#     def __init__(self, fmt=None, datefmt=None, info_fmt=None, custom_fmt=None, *args, **kwargs):
#         super().__init__(fmt, datefmt, *args, **kwargs)
#         self.default_fmt = fmt
#         self.info_fmt = info_fmt
#         self.custom_fmt = custom_fmt

#     def format(self, record):
#         # Use different format for INFO level
#         if record.levelno == logging.INFO:
#             self._style._fmt = self.info_fmt
#         # Use different format for CUSTOM level
#         elif record.levelno == CUSTOM_LEVEL_NUM:
#             self._style._fmt = self.custom_fmt
#             record.levelname = CUSTOM_LEVEL_NAME  # Ensure the custom level name is used
#         else:
#             self._style._fmt = self.default_fmt
#         return super().format(record)


# def init_logging(log_file: str) -> logging.Logger:
#     logging.addLevelName(CUSTOM_LEVEL_NUM, CUSTOM_LEVEL_NAME)
#     logging.Logger.custom = custom
#     logger = logging.getLogger('')
#     logger.setLevel(DEFAULT_LOGGING_LEVEL)
#     formatter = CustomFormatter(fmt=default_format, info_fmt=info_format, custom_fmt=custom_format, datefmt="%Y-%m-%d %H:%M:%S")
#     logging_handlers = setup_logging_handlers(log_file)
#     for handler in logging_handlers:
#         handler.setFormatter(formatter)
#         logger.addHandler(handler)
#     return logger


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
    parser.add_argument(
        '-v', '--version', action='version',
        version=f'{parser.prog} version 1.0.0'
    )
    parser.add_argument(
        '-s', '--setup_mode',
        action='store_true',
        help='setup mode, detect voltage levels and create config file'
    )
    parser.add_argument(
        '-t', '--test_mode',
        action='store_true',
        help='test mode, send pushbullet broadcast test'
    )
    parser.add_argument(
        '-w', '--washer_plug_name', metavar='',
        help='specifies washer plug name'
    )
    parser.add_argument(
        '-d', '--dryer_plug_name', metavar='',
        help='specifies dryer plug name'
    )
    parser.add_argument(
        '-l', '--log_file_name', metavar='',
        help='specifies custom log file name'
    )
    parser.add_argument(
        '-a', '--access_token', metavar='',
        help='specifies pushbullet access token'
    )
    parser.add_argument(
        '-c', '--channel_tag', metavar='',
        help='specifies pushbullet channel tag'
    )
    parser.add_argument(
        '-n', '--notifier_script', metavar='',
        help='user defined script to allow customized notifications'
    )
    return parser


def main() -> None:
    global log_file, logger, setup_mode, access_token, pbb

    plugs: list[AppliancePlugInfo] = []
    notifier_script: str = None
    run_mode: RunMode = RunMode.NORMAL

    parser = init_argparse()
    args = parser.parse_args()

    # set up default logging
    if args.log_file_name != None:
        log_file = args.log_file_name
    if args.washer_plug_name != None:
        plugs.append(AppliancePlugInfo(ApplianceType.WASHER, args.washer_plug_name))
    if args.dryer_plug_name != None:
        plugs.append(AppliancePlugInfo(ApplianceType.DRYER, args.dryer_plug_name))
    if args.setup_mode != None:
        if args.setup_mode:
            run_mode = RunMode.SETUP
    if args.access_token != None:
        access_token = args.access_token
    if args.channel_tag != None:
        channel_tag = args.channel_tag
    if args.notifier_script != None:
        notifier_script = args.notifier_script
    if args.test_mode != None:
        if args.test_mode:
            run_mode = RunMode.TEST

    logger = init_logging(log_file)


    if access_token == None or channel_tag == None:
        logger.warning(f"main: no access_token and/or channel_token, cannot send pushbullet notifications")
    else:
        logger.info(f"pbb: access_token: {access_token}, channel_tag: {channel_tag}")
        pbb = PushbulletBroadcaster(access_token, channel_tag)
    
    logger.custom(f'>>>>> START washer_plug_name: {plugs}, run_mode: {run_mode}, pushbullet: {pbb} <<<<<')
    success = asyncio.run(main_loop(run_mode=run_mode, plug_names=plugs, notifier_script=notifier_script))
    logger.custom(f'>>>>> FINI <<<<< success: {success}')

if __name__ == '__main__':
    main()