import pytest
import asyncio
import responses
import logging
from requests.models import Response
from scripts.washer_dryer_notifier import (
    PushbulletBroadcaster,
    AppliancePlugInfo,
    AppliancePlug,
    Appliance,
    notify_finished,
    ApplianceMode,
    main_loop,
)
# Importing the module to enable monkeypatching of functions by module name
import scripts.washer_dryer_notifier as notifier

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

# --- Tests for PushbulletBroadcaster --- #

@responses.activate
def test_pushbullet_broadcaster_send_notification():
    # Set up the responses mock for the Pushbullet API endpoint
    responses.add(
        responses.POST,
        "https://api.pushbullet.com/v2/pushes",
        json={"success": True},
        status=200,
    )
    # Instantiate the broadcaster with dummy values
    broadcaster = PushbulletBroadcaster(access_token="dummy_token", channel_tag="dummy_channel")
    try:
        broadcaster.send_notification("Test Title", "Test Message")
    except Exception as e:
        pytest.fail(f"send_notification() raised an exception: {e}")
    # Assert the request was made as expected
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call.request.url == "https://api.pushbullet.com/v2/pushes"
    assert call.response.status_code == 200

# --- Dummy Appliance for Testing main_loop --- #

class DummyAppliance(Appliance):
    async def query(self) -> ApplianceMode:
        # For testing, immediately simulate that the appliance finished its cycle
        self.set_appliance_mode(ApplianceMode.FINISHED)
        return ApplianceMode.FINISHED

    async def get_power(self) -> float:
        # Return a dummy power value (idle power)
        return self.appliance_plug.appliance_plug.emeter_realtime.power

# --- Tests for Main Loop Modes --- #

@pytest.mark.asyncio
async def test_main_loop_setup_mode(monkeypatch):
    """
    Test main_loop with setup_mode True (-s switch).
    We bypass device discovery by monkeypatching verify_appliances and read_config_file.
    """
    # Create a dummy smart device and associated appliance plug info
    dummy_device = DummySmartDevice(alias="DummyPlug", power=1.0, is_on=True)
    dummy_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="DummyPlug")
    dummy_appliance_plug = AppliancePlug(dummy_plug_info, dummy_device)
    dummy_appliance = DummyAppliance(dummy_appliance_plug)

    # Monkeypatch verify_appliances to return our dummy appliance
    async def dummy_verify_appliances(appliance_plug_infos):
        return [dummy_appliance]
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)

    # Monkeypatch read_config_file to do nothing
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    # Call main_loop in setup mode
    result = await asyncio.wait_for(main_loop(True, []), timeout=10)
    assert result is True

@pytest.mark.asyncio
async def test_main_loop_non_setup_mode(monkeypatch):
    """
    Test main_loop without setup mode.
    We bypass device discovery by monkeypatching verify_appliances and read_config_file.
    """
    # Create a dummy smart device and associated appliance plug info
    dummy_device = DummySmartDevice(alias="DummyPlug", power=1.0, is_on=True)
    dummy_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.DRYER, appliance_plug_name="DummyPlug")
    dummy_appliance_plug = AppliancePlug(dummy_plug_info, dummy_device)
    dummy_appliance = DummyAppliance(dummy_appliance_plug)

    # Monkeypatch verify_appliances to return our dummy appliance
    async def dummy_verify_appliances(appliance_plug_infos):
        return [dummy_appliance]
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)

    # Monkeypatch read_config_file to do nothing
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    # Call main_loop in non-setup mode
    result = await asyncio.wait_for(main_loop(False, []), timeout=10)
    assert result is True
