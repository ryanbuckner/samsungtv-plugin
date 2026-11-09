<img src="https://www.zdnet.com/wp-content/uploads/sites/3/samsung-logo_ce674d.jpg" hight="25%" width="25%" >

[![License](https://img.shields.io/badge/license-MIT-blue.svg?style=flat)](http://mit-license.org)
[![Platform](https://img.shields.io/badge/Platform-Indigo-blueviolet)](https://www.indigodomo.com/) 
[![Language](https://img.shields.io/badge/Language-python%203.13-orange)](https://www.python.org/)
[![Requirements](https://img.shields.io/badge/Requirements-Indigo%20v2022.1%2B-green)](https://www.indigodomo.com/downloads.html)
![Releases](https://img.shields.io/github/release-date/ryanbuckner/samsungtv-plugin?color=red&label=latest%20release)


# Samsung TV Plugin for Indigo Domotics Home Automation

Control your Samsung Smart TVs from [Indigo](http://www.indigodomo.com): turn them on and off, change volume, inputs, channels and apps, and use the TV's state in triggers, schedules and Control Pages.

It works with Samsung Smart TVs from **2013 and earlier through today's models**, entirely on your home network. No SmartThings account or cloud service needed.

This plugin is not endorsed by or affiliated with Samsung.

---

### What's New in 2026.1.1

- **TVs that change IP address keep working.** If your router gives a TV a new address, the plugin finds it on your network and updates the device for you. You can also run a trigger when that happens. It's on by default, and you can turn it off in the plugin settings.
- **Logging follows your setting.** With Debug Logging on *Critical*, the event log only shows errors and normal activity. No more stray library messages.
- **Quieter, steadier devices.** A TV no longer briefly restarts in Indigo every time the plugin learns something new about it.

### What's New in 2026.1.0

- **Older TVs now work.** 2014–2015 TVs (model numbers with an **H** or **J**, like UN48**J**U6400) and pre-2014 TVs can now be added. Some 2016–2017 TVs that used to show as permanently "unreachable" now work too.
- **Easier setup.** Click **Detect TV**, then **Pair / Authorize**, in the device settings. The plugin works out the rest.
- **"Off" is just off.** A TV that's turned off shows as *off* instead of with a red error badge.
- **More ways to turn a TV on.** If your TV doesn't wake up over the network, have Indigo run any action group instead (an IR blaster, your AV receiver, an Apple TV or Roku over HDMI‑CEC…).
- **Faster updates.** After you turn a TV on or off, Indigo shows the new state within seconds.
- **New actions:** Change Channel, Send Key Sequence, Press and Hold Key, Set Mute (on/off), Open URL in TV Browser, Send Text, Refresh Installed Apps.
- **Launch App** now lists the apps actually installed on your TV.
- **Frame TVs** now turn off completely instead of going into Art Mode.
- **Discovery** finds more TVs on your network.

---

### What You Can Do

- **Power:** turn on, turn off, toggle. Works with Indigo's standard On/Off buttons and Control Pages.
- **Volume:** up, down, set an exact level, mute on/off.
- **Inputs:** switch to TV, HDMI 1–4, AV or Component.
- **Channels:** go straight to a channel number (`7`, `704`, or `7-1`), channel up/down, previous channel.
- **Apps:** launch Netflix, YouTube, Plex or any app installed on the TV, and see which app is on screen.
- **Remote buttons:** every button on the Samsung remote (arrows, Enter, Back, Home, Menu, Guide, Info, color buttons, playback controls…), single presses or whole sequences.
- **Automation:** use the TV's state in triggers, for example "when the Living Room TV turns on, dim the lights", or "when Netflix starts, close the shades".

#### What works on your TV

| | 2016 and newer | 2014–2015 (H / J models) | 2013 and older |
|---|:-:|:-:|:-:|
| On/off status, volume and mute level | ✅ | ✅ | ✅ |
| Turn on / off | ✅ | ✅ | ✅ |
| Volume, mute, remote buttons, channels, inputs | ✅ | ✅ | ✅ |
| Launch apps / see the current app | ✅ | — | — |
| Send Text, Open URL, Press and Hold | ✅ | — | — |

Not sure what year your TV is? The plugin tells you when you click **Detect TV**.

---

### Installation

Download `SamsungTV.indigoPlugin` and double-click it to install.

### Adding a TV

1. **Turn the TV on.**
2. In Indigo, create a new device. Choose **SamsungTV** → **Samsung TV**.
3. Enter the TV's **IP address**. You'll find it on the TV under Settings → General → Network → Network Status. If the address changes later, the plugin follows it automatically, but giving the TV a fixed address in your router is still best.
4. Click **Detect TV**. You should see your TV's name and model.
5. Click **Pair / Authorize**:
   - **Most TVs:** a prompt appears on the TV. Press **Allow** with the TV remote within 30 seconds. The Indigo event log confirms when it worked.
   - **2014–2015 TVs:** a PIN appears on the TV. Type it into the **PIN** box and click **Submit PIN**.
6. Click **Save**.

That's it. The TV's status shows in Indigo within a few seconds.

> **The TV never asked me to Allow, or said no?** On the TV, open **Settings → General → External Device Manager → Device Connection Manager**. Allow the Indigo entry (or delete it and pair again). Setting **Access Notification** to *First time only* stops repeat prompts.

#### Optional device settings

- **Turn On using:** how Indigo turns the TV on. The default, **Wake-on-LAN**, wakes the TV over the network. Many older TVs, especially on Wi‑Fi, can't be woken that way. For those, pick an action group that turns the TV on another way:
  - an IR blaster sending the TV's power code;
  - turning on an Apple TV, Roku or AV receiver connected by HDMI. With HDMI‑CEC ("Anynet+" on Samsung) enabled, that wakes the TV.
- **Detect Running App:** on by default. Turn it off if you don't need the *current app* state.
- **Show advanced settings:** only needed if something unusual is going on (see Troubleshooting).

#### Plugin settings (Plugins → SamsungTV → Configure…)

- **Refresh frequency:** how often Indigo checks each TV (default 30 seconds).
- **Debug Logging:** leave on *Critical* unless you're troubleshooting.
- **Follow IP address changes:** on by default. If a TV stops answering at its address, the plugin looks for it on your network and updates the device when it finds it.
- **Look for a missing TV every:** 15, 30, 45 or 60 minutes (default 15). The plugin also looks right away when a different device shows up at a TV's address, and right after you turn a TV on.

> A TV can only be found while it's turned on. If a TV is off, the plugin finds it the next time someone turns it on.

---

### Using the TV in Indigo

#### Buttons and Control Pages
The TV works like any on/off device. The standard **Turn On**, **Turn Off** and **Toggle** buttons work in the Indigo client, on Control Pages, and anywhere else Indigo controls devices.

#### Actions
Add these to Action Groups, Triggers and Schedules (choose **SamsungTV** as the action type):

| Action | What it does |
|--------|-------------|
| Turn On / Turn Off / Toggle TV On/Off | Power control |
| Set Volume | Jump to an exact volume, 0–100 |
| Increase / Decrease Volume | One step up or down |
| Set Mute | Mute on, off, or toggle |
| Select Source / Input | TV, HDMI 1–4, AV, Component |
| Change Channel | Go to a channel number, e.g. `7`, `704`, `7-1` |
| Launch App | Open an app installed on the TV (or enter an app ID) |
| Send Remote Command | Press one remote button, picked from a list |
| Send Key Sequence | Press several buttons in a row, e.g. `HOME, RIGHT, RIGHT, ENTER`, with repeats and a pause between presses |
| Press and Hold Key | Hold a button down for a few seconds |
| Send Key Code | Press any button by its Samsung code, e.g. `KEY_RED` |
| Send Text | Type into a search box on the TV |
| Open URL in TV Browser | Open a web page on the TV |
| Refresh Installed Apps | Reload the app list after installing new apps on the TV |
| Refresh Data | Check the TV's status right now |
| Run Diagnostics | Check the connection to this TV and explain any problem in the event log |

#### Device states (for triggers and Control Pages)

| State | Example values |
|-------|----------------|
| Power Status | `on`, `standby`, `off` |
| Volume Level | `0`–`100` |
| Is Muted | true / false |
| Current App | `Netflix`, `YouTube`, … |
| Current Source | `HDMI 1`, `TV`, `Smart Hub` (an app is running) |
| Model | `QN65Q80T`, `UN48JU6400`, … |
| Previous IP Address | the TV's address before it last changed |
| IP Address Changed At | when the plugin last updated the TV's address |

**Example trigger:** *Device State Changed → Living Room TV → Current App → becomes equal to → Netflix* → run your "Movie Mode" action group.

#### Triggers

| Trigger | When it fires |
|---------|---------------|
| TV IP Address Changed | The plugin found a TV at a new IP address and updated it. Pick one TV or *Any Samsung TV*. |

**Example:** *SamsungTV → TV IP Address Changed → Any Samsung TV* → send yourself an email or push notification, as a reminder to reserve the TV's address in your router. The TV's **Previous IP Address** state tells you which address it moved from.

#### Plugins menu

| Menu Item | What it does |
|-----------|-------------|
| Discover Samsung TVs on Network | Finds Samsung TVs that are turned on and lists their IP addresses in the event log (takes about 20 seconds) |
| Run Diagnostics on All TVs | Checks every configured TV and explains any problem in the event log |
| List Configured Device Info | Lists each TV's IP, model and MAC address in the event log |
| Toggle Debug / Toggle Detailed State Logging | Extra logging for troubleshooting |

---

### Scripting

You can also control TVs from Indigo Python scripts:

```python
plugin = indigo.server.getPlugin("com.ryanbuckner.indigoplugin.samsungtv")
tv     = indigo.devices["Living Room TV"]   # your device name

# Power
indigo.device.turnOn(tv.id)
indigo.device.turnOff(tv.id)

# Volume and mute
plugin.executeAction("setVolume", deviceId=tv.id, props={"volumeLevel": "20"})
plugin.executeAction("setMute",   deviceId=tv.id, props={"muteMode": "on"})   # on / off / toggle

# Inputs, channels and apps
plugin.executeAction("selectSource",  deviceId=tv.id, props={"sourceId": "HDMI 1"})
plugin.executeAction("changeChannel", deviceId=tv.id, props={"channel": "7-1"})
plugin.executeAction("launchApp",     deviceId=tv.id, props={"appName": "Netflix"})

# Remote buttons
plugin.executeAction("sendCommand",     deviceId=tv.id, props={"commandCode": "home"})
plugin.executeAction("sendKeySequence", deviceId=tv.id,
                     props={"keySequence": "HOME, RIGHT, RIGHT, ENTER", "repeats": "1", "delay": "0.5"})

# Read the TV's state
print(tv.onState, tv.states["status"], tv.states["volumeLevel"], tv.states["currentApp"])
```

**Example: movie night**

```python
plugin = indigo.server.getPlugin("com.ryanbuckner.indigoplugin.samsungtv")
tv     = indigo.devices["Living Room TV"]

if not tv.onState:
    indigo.device.turnOn(tv.id)
    indigo.server.sleep(10)          # give the TV time to start up

plugin.executeAction("selectSource", deviceId=tv.id, props={"sourceId": "HDMI 1"})
plugin.executeAction("setVolume",    deviceId=tv.id, props={"volumeLevel": "20"})
```

---

### Troubleshooting

**Start with Plugins → SamsungTV → Run Diagnostics on All TVs.** It checks each TV and writes a plain-English explanation of any problem to the event log.

- **The TV shows "off" but it's on.** The TV's IP address has probably changed. With **Follow IP address changes** on, the plugin finds it within your chosen search interval (or right away when you turn the TV on from Indigo). Otherwise, check the address on the TV (Settings → General → Network → Network Status) and update the device. Either way, reserving the address in your router prevents it.
- **The TV shows an "IP changed" badge.** A different device is now using this TV's old address. The plugin is already looking for the TV, and it updates the device as soon as the TV is on.
- **The TV shows a red "no response" badge.** Indigo has never been able to reach the TV. Usually the IP address is wrong, or the TV was off when you added it. Turn the TV on and click **Detect TV** again.
- **Status works but buttons don't.** The TV hasn't been paired. Edit the device, click **Pair / Authorize**, and accept the prompt (or enter the PIN) on the TV.
- **Turn On doesn't work.** Many TVs, especially 2015 and older on Wi‑Fi, can't be woken over the network. Set **Turn On using** to an action group that turns the TV on another way (see *Optional device settings*). On newer TVs, also check that **Settings → General → Network → Expert Settings → Power On with Mobile** is enabled.
- **The TV keeps asking to Allow.** On the TV, set **Device Connection Manager → Access Notification** to *First time only*.
- **Discovery doesn't find my TV.** The TV must be fully on and on the same network as the Indigo server. You can always add a TV by its IP address instead.

Still stuck? Set **Debug Logging** to *Debug* in the plugin settings, reproduce the problem, and post the event log on the [Indigo forum](https://forums.indigodomo.com/viewforum.php?f=363).

