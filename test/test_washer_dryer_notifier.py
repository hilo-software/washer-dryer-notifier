import pytest
import asyncio
import responses
import logging
from scripts.washer_dryer_notifier import (
    PushbulletBroadcaster,
    AppliancePlugInfo,
    AppliancePlug,
    Appliance,
    notify_finished,
    ApplianceMode,
    RunMode,
    main_loop,
    pbb,
)
from unittest.mock import MagicMock
import scripts.washer_dryer_notifier as notifier
import pdb

# --- Extend dummy_logger with a custom method --- #
CUSTOM_LEVEL_NUM = 25
CUSTOM_LEVEL_NAME = "CUSTOM"
logging.addLevelName(CUSTOM_LEVEL_NUM, CUSTOM_LEVEL_NAME)

def dummy_custom(self, message, *args, **kwargs):
    if self.isEnabledFor(CUSTOM_LEVEL_NUM):
        self._log(CUSTOM_LEVEL_NUM, message, args, **kwargs)

# Add the custom method to the Logger class
logging.Logger.custom = dummy_custom

# Create and configure the dummy_logger
dummy_logger = logging.getLogger("dummy")
dummy_logger.addHandler(logging.StreamHandler())
dummy_logger.setLevel(CUSTOM_LEVEL_NUM)

# --- No-op sleep to avoid timeouts --- #
async def no_sleep(duration):
    return None

# --- Dummy Classes for Testing Appliance --- #
class DummyEmeter:
    def __init__(self, power):
        self.power = power

class DummySmartDevice:
    def __init__(self, alias, power=1.0, is_on=True):
        self.alias = alias
        self.emeter_realtime = DummyEmeter(power)
        self.is_on = is_on

    async def update(self):
        # Simulate a no-op update
        return

    async def turn_on(self):
        self.is_on = True
        return

# --- Dummy Appliance for Testing main_loop Setup Mode --- #
class DummyApplianceForSetup(Appliance):
    def __init__(self, plug):
        super().__init__(plug)
        self.call_count = 0

    async def get_power(self) -> float:
        self.call_count += 1
        # On first call, simulate idle power (1.0), then running power (3.0)
        return 1.0 if self.call_count == 1 else 3.0

    async def query(self) -> ApplianceMode:
        # For this test, query is not used in setup mode
        return self.appliance_mode
    
# --- Tests for PushbulletBroadcaster --- #
@responses.activate
def test_pushbullet_broadcaster_send_notification(monkeypatch):
    responses.add(
        responses.POST,
        "https://api.pushbullet.com/v2/pushes",
        json={"success": True},
        status=200,
    )
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)
    broadcaster = PushbulletBroadcaster(access_token="dummy_token", channel_tag="dummy_channel")
    try:
        broadcaster.send_notification("Test Title", "Test Message")
    except Exception as e:
        pytest.fail(f"send_notification() raised an exception: {e}")
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call.request.url == "https://api.pushbullet.com/v2/pushes"
    assert call.response.status_code == 200

# --- Dummy Appliance for Testing main_loop --- #
class DummyAppliance(Appliance):
    async def query(self) -> ApplianceMode:
        self.set_appliance_mode(ApplianceMode.FINISHED)
        return ApplianceMode.FINISHED

    async def get_power(self) -> float:
        return self.appliance_plug.appliance_plug.emeter_realtime.power

# --- Tests for Main Loop Modes --- #
@pytest.mark.asyncio
async def test_main_loop_setup_mode_no_appliances(monkeypatch):
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)
    # Patch asyncio.sleep with our no_sleep function to avoid delays
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    dummy_device = DummySmartDevice(alias="DummyPlug", power=1.0, is_on=True)
    dummy_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="DummyPlug")
    dummy_appliance_plug = AppliancePlug(dummy_plug_info, dummy_device)
    dummy_appliance = DummyAppliance(dummy_appliance_plug)

    async def dummy_verify_appliances(appliance_plug_infos):
        return [dummy_appliance]
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    result = await asyncio.wait_for(main_loop(RunMode.SETUP, []), timeout=10)
    assert result is False

@pytest.mark.asyncio
async def test_main_loop_setup_mode_with_appliances(monkeypatch):
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)
    # Patch asyncio.sleep with our no_sleep function to avoid delays
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    dummy_washer_device = DummySmartDevice(alias="washer", power=1.0, is_on=True)
    dummy_washer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="washer")
    dummy_washer_appliance_plug = AppliancePlug(dummy_washer_plug_info, dummy_washer_device)
    dummy_washer_appliance = DummyApplianceForSetup(dummy_washer_appliance_plug)

    dummy_dryer_device = DummySmartDevice(alias="dryer", power=1.0, is_on=True)
    dummy_dryer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.DRYER, appliance_plug_name="dryer")
    dummy_dryer_appliance_plug = AppliancePlug(dummy_dryer_plug_info, dummy_dryer_device)
    dummy_dryer_appliance = DummyApplianceForSetup(dummy_dryer_appliance_plug)

    appliances: list[DummyAppliance] = [dummy_washer_appliance, dummy_dryer_appliance]
    appliance_plug_infos = [dummy_washer_plug_info, dummy_dryer_plug_info]

    async def dummy_verify_appliances(appliance_plug_infos):
        return appliances
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    result = await asyncio.wait_for(main_loop(RunMode.SETUP, appliance_plug_infos), timeout=10)
    assert result is True

@pytest.mark.asyncio
async def test_main_loop_test_mode(monkeypatch):
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)
    # Patch asyncio.sleep with our no_sleep function to avoid delays
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    # Mock PushbulletBroadcaster and set it as the global `pbb`
    mock_pbb = MagicMock()
    monkeypatch.setattr(notifier, "pbb", mock_pbb)

    dummy_washer_device = DummySmartDevice(alias="washer", power=1.0, is_on=True)
    dummy_washer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="washer")
    dummy_washer_appliance_plug = AppliancePlug(dummy_washer_plug_info, dummy_washer_device)
    dummy_washer_appliance = DummyApplianceForSetup(dummy_washer_appliance_plug)

    dummy_dryer_device = DummySmartDevice(alias="dryer", power=1.0, is_on=True)
    dummy_dryer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.DRYER, appliance_plug_name="dryer")
    dummy_dryer_appliance_plug = AppliancePlug(dummy_dryer_plug_info, dummy_dryer_device)
    dummy_dryer_appliance = DummyApplianceForSetup(dummy_dryer_appliance_plug)

    appliances: list[DummyAppliance] = [dummy_washer_appliance, dummy_dryer_appliance]
    appliance_plug_infos = [dummy_washer_plug_info, dummy_dryer_plug_info]

    async def dummy_verify_appliances(appliance_plug_infos):
        return appliances

    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    # Run the main loop in TEST mode
    result = await asyncio.wait_for(main_loop(RunMode.TEST, appliance_plug_infos, 10), timeout=10)

    # Ensure the test passes
    assert result is True
    # Verify that send_notification was called at least once
    mock_pbb.send_notification.assert_called()


@pytest.mark.asyncio
async def test_main_loop_non_setup_mode_no_appliances(monkeypatch):
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)

    dummy_device = DummySmartDevice(alias="DummyPlug", power=1.0, is_on=True)
    dummy_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.DRYER, appliance_plug_name="DummyPlug")
    dummy_appliance_plug = AppliancePlug(dummy_plug_info, dummy_device)
    dummy_appliance = DummyAppliance(dummy_appliance_plug)

    async def dummy_verify_appliances(appliance_plug_infos):
        return [dummy_appliance]
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    result = await asyncio.wait_for(main_loop(RunMode.NORMAL, []), timeout=10)
    assert result is False

@pytest.mark.asyncio
async def test_main_loop_non_setup_mode_with_appliances(monkeypatch):
    # Patch logger to avoid NoneType errors
    monkeypatch.setattr(notifier, "logger", dummy_logger)
    # Patch asyncio.sleep with our no_sleep function to avoid delays
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    dummy_washer_device = DummySmartDevice(alias="washer", power=1.0, is_on=True)
    dummy_washer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="washer")
    dummy_washer_appliance_plug = AppliancePlug(dummy_washer_plug_info, dummy_washer_device)
    dummy_washer_appliance = DummyApplianceForSetup(dummy_washer_appliance_plug)

    dummy_dryer_device = DummySmartDevice(alias="dryer", power=1.0, is_on=True)
    dummy_dryer_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.DRYER, appliance_plug_name="dryer")
    dummy_dryer_appliance_plug = AppliancePlug(dummy_dryer_plug_info, dummy_dryer_device)
    dummy_dryer_appliance = DummyApplianceForSetup(dummy_dryer_appliance_plug)

    appliances: list[DummyAppliance] = [dummy_washer_appliance, dummy_dryer_appliance]
    appliance_plug_infos = [dummy_washer_plug_info, dummy_dryer_plug_info]

    async def dummy_verify_appliances(appliance_plug_infos):
        return appliances
    
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    # pdb.set_trace()
    result = await asyncio.wait_for(main_loop(RunMode.NORMAL, appliance_plug_infos, 10), timeout=10)
    assert result is True
