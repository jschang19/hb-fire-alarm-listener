FSKJ222 FIRE ALARM LISTENER FOR macOS
=====================================

Purpose
-------
This is a standalone macOS listener for a NOHMI FSKJ222-style smoke alarm.
It is independent from the existing doorbell-listener project: separate folder,
virtualenv, LaunchAgent, log files, detector state, and Homebridge accessory.

The detector does NOT use Japanese speech recognition. It recognizes the alarm's
acoustic fingerprint measured from both supplied recordings:

  - sustained preamble: about 2.70 kHz
  - two rising tonal sweeps: about 0.8 s each
  - sweep-start interval: about 1.26 s
  - language/voice segment after the tones: ignored

Production thresholds are deliberately structural rather than volume-only:

  - preamble 2620-2780 Hz for >= 0.35 s
  - each sweep 0.68-0.98 s
  - sweep slope 3000-4100 Hz/s
  - sweep R^2 >= 0.95
  - sweep span >= 2200 Hz
  - >= 90% monotonic progression
  - two sweep starts 1.10-1.45 s apart
  - strong narrow-band prominence/concentration requirements

This rejects ordinary speech/music, fixed beeps, white noise, a single sweep,
two sweeps with no valid preamble, wrong sweep rates, and wrong sweep spacing.

IMPORTANT SAFETY LIMIT
----------------------
This is only a secondary notification bridge. It is not a certified life-safety
system and must never replace the physical smoke alarm or required fire-safety
equipment.

Any purely acoustic detector can be fooled if a television/speaker plays a
recording of the exact same FSKJ222 alarm loudly enough. If that is a realistic
risk in your room, place the Mac/microphone closer to the physical alarm and use
--confirm-cycles 2 after validating that the real alarm is still detected.

Files
-----
  fire_alarm_listener.py                 production listener
  regression_test.py                     offline regression suite
  fskj222_room_16k.wav                   your living-room sample, test fixture
  fskj222_direct_16k.wav                 full Japanese sample, test fixture
  local.fire-alarm-listener.plist.template
  setup.sh
  install-service.sh
  uninstall-service.sh
  homebridge-smoke.sh                    on/off/status helper
  homebridge-snippet.json                merged config example
  requirements.txt

Homebridge configuration
------------------------
Your existing HttpWebHooks child bridge can contain both the current doorbell
and the new smoke sensor. The supplied homebridge-snippet.json is the merged
block. The new part is:

  "sensors": [
    {
      "id": "livingRoomFireAlarm",
      "name": "客廳火警警報",
      "type": "smoke"
    }
  ]

Keep:

  "webhook_listen_host": "127.0.0.1"

because the detector runs on the same Mac. That keeps port 51828 local-only.

Installation
------------
1. Extract this package, then:

     cd ~/Downloads/fire-alarm-listener
     chmod +x setup.sh
     ./setup.sh

   setup.sh installs a completely separate project at:

     ~/fire-alarm-listener

2. Add the smoke sensor to the Homebridge HttpWebHooks config and restart that
   child bridge.

3. Verify the Homebridge accessory before involving audio:

     ~/fire-alarm-listener/homebridge-smoke.sh status
     ~/fire-alarm-listener/homebridge-smoke.sh on
     ~/fire-alarm-listener/homebridge-smoke.sh off

   The Home app should show 客廳火警警報 as a smoke sensor.

4. Run offline regression:

     cd ~/fire-alarm-listener
     .venv/bin/python regression_test.py

   Expected final line:

     All 26 regression tests passed.

5. Run the microphone detector manually once. This is important so macOS can
   request/verify microphone permission for Python:

     ~/fire-alarm-listener/.venv/bin/python \
       ~/fire-alarm-listener/fire_alarm_listener.py \
       --debug --no-homebridge

   Use the physical alarm's TEST function or play the supplied reference sample.
   A complete match prints "complete_alarm_cycle". Press Ctrl-C when finished.

6. Test end-to-end, still in the foreground:

     ~/fire-alarm-listener/.venv/bin/python \
       ~/fire-alarm-listener/fire_alarm_listener.py --debug

   A complete alarm cycle should set HomeKit smoke=true. After 30 seconds with
   no further complete alarm cycle, it sends smoke=false.

7. Install the independent LaunchAgent:

     ~/fire-alarm-listener/install-service.sh

   It uses RunAtLoad + KeepAlive and starts again after login/reboot.

Operations / troubleshooting
----------------------------
Status:

  launchctl print gui/$(id -u)/local.fire-alarm-listener

Follow normal log:

  tail -f ~/Library/Logs/fire-alarm-listener.log

Follow error log:

  tail -f ~/Library/Logs/fire-alarm-listener.err.log

Restart service:

  launchctl kickstart -k gui/$(id -u)/local.fire-alarm-listener

Stop/remove LaunchAgent (project files remain):

  ~/fire-alarm-listener/uninstall-service.sh

List microphone devices:

  ~/fire-alarm-listener/.venv/bin/python \
    ~/fire-alarm-listener/fire_alarm_listener.py --list-devices

If the default input is wrong, test with:

  ~/fire-alarm-listener/.venv/bin/python \
    ~/fire-alarm-listener/fire_alarm_listener.py --device DEVICE_NUMBER --debug --no-homebridge

Then add these two ProgramArguments to the plist template before reinstalling:

  <string>--device</string>
  <string>DEVICE_NUMBER</string>

False-positive hardening
------------------------
Default is --confirm-cycles 1. One full FSKJ222 pattern already requires the
2.70 kHz preamble + both correctly shaped sweeps and takes about 3 seconds.

If your television is physically close to the Mac and ever causes a real false
alarm, change the value after --confirm-cycles in
local.fire-alarm-listener.plist.template from 1 to 2, then run:

  ~/fire-alarm-listener/install-service.sh

Mode 2 requires two complete FSKJ222 cycles about 5 seconds apart. This is much
harder for incidental TV/program audio to satisfy, but adds about 5 seconds of
notification latency and should only be enabled after a real-alarm test.

State behavior
--------------
- smoke=true only after the configured number of complete alarm cycles.
- While active, complete repeated cycles refresh the alarm evidence timer.
- smoke=false after 30 seconds without another complete cycle.
- On service startup, a quiet 20-second period sends one false update to clear a
  stale HomeKit state left by a prior crash/reboot.
- Homebridge updates are serialized; if localhost Homebridge is temporarily
  unavailable, the latest desired state is retried rather than silently lost.
