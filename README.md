# Washer and Dryer Notifier
## Summary
- Python script which works with a TP-Link Smart Plug and the PushBullet service to sense when washers and dryers are active and send notifications to subscribers when the machine(s) are done.
## Purpose
- Allow efficient time use of the washer and dryer, especially if the machines are located where the user cannot conveniently see/hear them.
- Reduce the chance of forgetting to dry clothes once the washer has finished, preventing mildew.
## Prerequisites
- TP-Link Smart Plug with Emeter (Energy meter) capability
  - The KP115 Smart Plug and the HS300 Smart Strip models are compatible
- The target plug must have an alias name assigned to it.
- Kasa python library to access TP-Link Smart Plug features from python
- Pushbullet account and phone app
- Python pushbullet.py
  - ``` pip install pushbullet.py ```
## How it works
- The washer and dryer must be plugged into TP-Link Smart Plugs.
  - Because of the high current draw of the washer and dryer, the suggested plug is model KP115.  Other plugs have not been tested.
- The script is intended to run continuously and will probe the smart plug(s) at regular intervals for activity indicated by an increased current draw.
- Once activity is detected the script then monitors for the current draw dropping to nominal levels, indicating the machine on the smart plug has finished.
  - At that point the script will send a notification via PushBullet to all subscribed smart phones.
## Usage
### Setup
- Plug appliance into appropriate smart plug.
- Turn on smart plug(s) and verify appliance(s) works.
- Turn off appliance(s), leaving smart plugs on.
- On any PC that supports command line Python 3.6.1 or above, run the washer_dryer_notifier.py script with the setup switch "-s" and also "-w" switch followed by the name of the smart plug that the washer is plugged into and the "-d" switch followed by the name of the smart plug that the dryer is plugged into.
  - Note that the -w and -d switches are optional, i.e., you can have only one or the other if you want.
- For example:
```
$ ./scripts/washer_dryer_notifier.py -s -w washer -d dryer
```
- Wait 30 seconds and turn on appliance(s).
- Leave appliances on for at least 1 minute, then turn applicance(s) off.
- Verify that a washer_dryer_notifier.config file is created.
### Continuous run
- Run the washer_dryer_notifier.py script as in Setup but without the "-s" switch.
- For example:
```
$ ./scripts/washer_dryer_notifier.py -w washer -d dryer
```

- This will run the script in continuous mode as described above.
