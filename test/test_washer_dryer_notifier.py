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
    main_loop,
)
import scripts.washer_dryer_notifier as notifier

# Dummy logger for tests
dummy_logger = logging.getLogger("dummy")
dummy_logger.addHandler(logging.StreamHandler())
dummy_logger.setLevel(logging.DEBUG)

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
    responses.add(
        responses.POST,
        "https://api.pushbullet.com/v2/pushes",
        json={"success": True},
        status=200,
    )
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

    dummy_device = DummySmartDevice(alias="DummyPlug", power=1.0, is_on=True)
    dummy_plug_info = AppliancePlugInfo(appliance_type=notifier.ApplianceType.WASHER, appliance_plug_name="DummyPlug")
    dummy_appliance_plug = AppliancePlug(dummy_plug_info, dummy_device)
    dummy_appliance = DummyAppliance(dummy_appliance_plug)

    async def dummy_verify_appliances(appliance_plug_infos):
        return [dummy_appliance]
    monkeypatch.setattr(notifier, "verify_appliances", dummy_verify_appliances)
    monkeypatch.setattr(notifier, "read_config_file", lambda appliances: None)

    result = await asyncio.wait_for(main_loop(True, []), timeout=10)
    assert result is False

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

    result = await asyncio.wait_for(main_loop(False, []), timeout=10)
    assert result is False
