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
import bisect

LOG_FILE = "washer_dryer_notifier.log"
CONFIG_FILE = "washer_dryer_notifier.config"
CUTOFF_POWER = 3.0
SETUP_PROBE_INTERVAL_SECS = 30
RUNNING_TIME_WAIT_SECS = 60
RUNNING_SETUP_RETRY_MAX = 5
PROBE_INTERVAL_SECS = 5 * 60
PLUG_SETTLE_TIME_SECS = 10
RETRY_MAX = 3
RETRY_SLEEP_DELAY = 30
IDLE_TAG = 'idle'
RUNNING_TAG = 'running'


class ApplianceType(Enum):
    WASHER = 0
    DRYER = 1


class ApplianceMode(Enum):
    IDLE = 1
    RUNNING = 2
    FINISHED = 3


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
    
    
    def set_appliance_idle_power(self, appliance_idle_power: float) -> None:
        self.appliance_idle_power = appliance_idle_power


    def get_appliance_idle_power(self) -> float:
        return self.appliance_idle_power
    

    def set_appliance_running_power(self, appliance_running_power: float) -> None:
        self.appliance_running_power = appliance_running_power
    

    def get_appliance_running_power(self) -> float:
        return self.appliance_running_power

    def query(self) -> ApplianceMode:
        return self.appliance_mode
    

    async def get_power(self) -> float:
        await self.appliance_plug.appliance_plug.update()
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
cutoff_power = CUTOFF_POWER

def fn_name():
    return inspect.currentframe().f_back.f_code.co_name

async def init(target_plug_name: str) -> SmartDevice:
    '''
    async function.  Uses kasa library to discover and find target device matching target_plug alias.

    Returns:
        True if plug is found
    '''
    found = await Discover.discover()
    for smart_device in found.values():
        await smart_device.update()
        if smart_device.alias == target_plug_name:
            if not smart_device.is_on:
                if not await turn_on(smart_device):
                    return None
                logger.info(f"plug: was off, now successfully turned on so we delay {PLUG_SETTLE_TIME_SECS} seconds to allow power to settle")
                await asyncio.sleep(PLUG_SETTLE_TIME_SECS)
                await smart_device.update()
            return smart_device
    return None

async def init_plugs(target_plug_infos: list[AppliancePlugInfo]) -> list[AppliancePlug]:
    '''
    async function.  Uses kasa library to discover and find target device(s) matching target_plug(s) alias.

    Returns:
        list of matching plugs
    '''
    matching_plugs: list[AppliancePlug] = []
    found = await Discover.discover()
    for smart_device in found.values():
        await smart_device.update()
        for target_plug_info in target_plug_infos:
            if smart_device.alias == target_plug_info.appliance_plug_name:
                if not smart_device.is_on:
                    if not await turn_on(smart_device):
                        logger.warning(f"WARNING: Unable to turn on plug: {target_plug_info.appliance_plug_name}")
                        continue
                    logger.info(f"plug: was off, now successfully turned on so we delay {PLUG_SETTLE_TIME_SECS} seconds to allow power to settle")
                    await asyncio.sleep(PLUG_SETTLE_TIME_SECS)
                    await smart_device.update()
                matching_plugs.append(AppliancePlug(target_plug_info, smart_device))
    return matching_plugs

async def turn_on(plug: SmartDevice) -> bool:
    await plug.turn_on()
    await plug.update()
    return plug.is_on

def get_power(plug: SmartDevice) -> float:
    return plug.emeter_realtime.power

def is_running(plug: SmartDevice) -> bool:
    global cutoff_power
    power: float = get_power(plug)
    logger.info(f"{fn_name()}: power: {power}")
    return power > cutoff_power

def notify_finished(appliance: Appliance) -> None:
    pass


def create_config_file(appliances: list[Appliance]) -> None:
    config = configparser.ConfigParser()
    for appliance in appliances:
        section_name = appliance.get_appliance_name()
        config.add_section(section_name)
        config.set(section_name, IDLE_TAG, str(appliance.get_appliance_idle_power()))
        config.set(section_name, RUNNING_TAG, str(appliance.get_appliance_running_power()))
    with open(CONFIG_FILE, "w") as config_file:
        config.write(config_file)


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
        logger.info(f"We have set the IDLE power: {idle_power} for the appliance: {appliance.get_appliance_name()}")
    logger.info(f"We have set the IDLE power for the appliance(s)")

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
            logger.info(f"We have set the RUNNING power for the appliance(s)")
            break

        logger.warning(f"At least one appliance failed to detect a valid RUNNING voltage, retry_count: {retry_count}")
        running_power_set = True
        retry_count += 1
        await asyncio.sleep(SETUP_PROBE_INTERVAL_SECS)
        elapsed_seconds += SETUP_PROBE_INTERVAL_SECS
        if elapsed_seconds > retry_seconds_max:
            logger.error(f"UNABLE to set running power in one or more appliances: {repr(appliances)}")
            break
    logger.info(f"setup_loop: running_power_set: {running_power_set}, retry_count: {retry_count}, elapsed_seconds: {elapsed_seconds}")
    #  if successful, create a config file
    if idle_power_set and running_power_set:
        create_config_file(appliances)
    else:
        return False
    return True

async def verify_appliance(appliance_plug_name: str) -> Union[Appliance, ApplianceException]:
    if appliance_plug_name == None:
        return None
    try:
        appliance_plug = await init(appliance_plug_name)
        if not appliance_plug:
            raise ApplianceException(f"Unable to init the appliance_plug: {appliance_plug_name}")
        appliance = Appliance(appliance_plug)
        appliances.append(appliance)
        return Appliance
    except Exception as e:
        logger.error(f"ERROR in verify_appliance: {e}")
    return None


async def verify_appliances(appliance_plug_infos: list[AppliancePlugInfo]) -> Union[list[Appliance], ApplianceException]:
    appliance_plugs: list[AppliancePlug] = await init_plugs(appliance_plug_infos)
    if len(appliance_plugs) != len(appliance_plug_infos):
        return []
    appliances: list[Appliance] = []
    for appliance_plug in appliance_plugs:
        appliances.append(Appliance(appliance_plug))
    return appliances


async def main_loop(setup_mode: bool, plug_names: list[AppliancePlugInfo]) -> bool:
    if len(plug_names) == 0:
        logger.error(f"ERROR, no washer or dryer specified, need at least one")
        return False
    appliances = await verify_appliances(plug_names)
    if len(appliances) == 0:
        logger.error(f"ERROR, no appliances verified")
        return False
    logger.info(f"setup_mode: {setup_mode}, appliances: {repr(appliances)}")
    if setup_mode:
        return await setup_loop(appliances)
    # main running loop forever
    retry_ct = 0
    normal_finish = False
    while retry_ct < RETRY_MAX:
        try:
            for appliance in appliances:
                if appliance.query() == ApplianceMode.FINISHED:
                    notify_finished(appliance)
        except Exception as e:
            # Treat this as a network issue, retry after sleep up to RETRY_MAX attempts
            retry_ct = retry_ct + 1
            logger.error(f'ERROR, unexpected exit from main_loop: {e}, retry_ct: {retry_ct}')
            await asyncio.sleep(RETRY_SLEEP_DELAY)
        await asyncio.sleep(PROBE_INTERVAL_SECS)
    return True

def setup_logging_handlers(log_file: str) -> list:
    try:
        logging_file_handler = logging.FileHandler(filename=log_file, mode='w')
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

def init_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    # Create formatter with the specified date format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    logging_handlers = setup_logging_handlers(log_file)
    for handler in logging_handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


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
    return parser


def main() -> None:
    global log_file, logger, setup_mode

    plugs: list[AppliancePlugInfo] = []

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
        setup_mode = args.setup_mode

    logger = init_logging(log_file)

    logger.info(f'>>>>> START washer_plug_name: {plugs}, setup_mode: {setup_mode} <<<<<')
    success = asyncio.run(main_loop(setup_mode, plugs))
    logger.info(f'>>>>> FINI <<<<<')

if __name__ == '__main__':
    main()