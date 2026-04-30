<img src="https://kalingatv.com/wp-content/uploads/2022/01/Tizen-app.jpg" hight="25%" width="25%" >

[![License](https://img.shields.io/badge/license-MIT-blue.svg?style=flat)](http://mit-license.org)
[![Platform](https://img.shields.io/badge/Platform-Indigo-blueviolet)](https://www.indigodomo.com/) 
[![Language](https://img.shields.io/badge/Language-python%203.13-orange)](https://www.python.org/)
[![Requirements](https://img.shields.io/badge/Requirements-Indigo%20v2022.1%2B-green)](https://www.indigodomo.com/downloads.html)
![Releases](https://img.shields.io/github/release-date/ryanbuckner/samsungtv-plugin?color=red&label=latest%20release)


# Samsung TV Plugin for Indigo Domotics Home Automation

This Indigo Plugin connects [Indigo Domotics](http://www.indigodomo.com) to Samsung Smart TVs running Tizen OS via the WebSocket / REST API. No SmartThings account or cloud dependency required.

This plugin is not endorsed by or affiliated with Samsung.

---

### Features

**Power**
- On / Off / Toggle via Wake-on-LAN and the TV WebSocket API
- Polled power state monitoring with configurable refresh interval

**Volume**
- Volume Up / Down / Mute
- Increase or decrease volume by 1 step
- Set Volume to an absolute level (0–100)
- Volume level read from the TV via UPnP on each poll

**Navigation**
- Full D-pad: Up, Down, Left, Right, Enter, Back, Home, Exit
- Menu, Source, Guide, Tools, Info

**Media Playback**
- Play, Pause, Stop, Fast Forward, Rewind
- Channel Up / Channel Down

**Source / Input Selection**
- Switch inputs (TV, HDMI 1–4, AV, Component) from an action dropdown

**App Launching & Tracking**
- Launch any app from a dropdown (Netflix, YouTube TV, YouTube, Prime Video, Disney+, and more)
- Polls for the currently running app and updates the `currentApp` device state

**Send Key Code**
- Send any raw Samsung key code directly (e.g. `KEY_RED`, `KEY_ASPECT`, `KEY_0`)

---

### Installation

Download `SamsungTV.indigoPlugin` and double-click it to install.

#### Plugin Config
- Set the poll interval (default 30 seconds, minimum 10)

#### Device Config
1. Create a new device, type **Samsung TV**
2. Enter the IP address of the TV
3. Port defaults to `8002` — change only if needed
4. Enter a unique filename for the token (e.g. `living-room-tv.txt`)

> The TV must be powered on the first time you add it so the plugin can obtain a WebSocket token and discover the MAC address for Wake-on-LAN.

> Developer mode is required on the TV. Authorize the IP address of your Indigo server when prompted on-screen. See [Samsung's official instructions for enabling Developer Mode](https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/tv-device.html).

---

### Actions

| Action | Description |
|--------|-------------|
| Turn On | Send Wake-on-LAN magic packet |
| Turn Off | Send power key via WebSocket |
| Toggle TV On/Off | WOL on, WebSocket power key off |
| Refresh Data | Force an immediate state poll |
| Increase Volume (+1) | Step volume up by one |
| Decrease Volume (-1) | Step volume down by one |
| Set Volume | Set an absolute volume level 0–100 |
| Send Remote Command | Pick a button from a dropdown (volume, nav, media, etc.) |
| Send Key Code | Type any `KEY_xxx` code directly |
| Select Source / Input | Switch to TV, HDMI 1–4, AV, or Component |
| Launch App | Launch an app from a dropdown |

---

### Plugins Menu

| Menu Item | Description |
|-----------|-------------|
| Toggle Debug | Enable / disable debug logging |
| Toggle Detailed State Logging | Log every device state value on each poll cycle |
| List Configured Device Info | Dump name, IP, MAC, and OS for all configured TVs to the log |
| Discover Samsung TVs on Network | ⚠️ Experimental — scan the network for Samsung TVs via SSDP |

---

### TV Discovery (Experimental)

> ⚠️ **Experimental feature.** Discovery works on most networks but results are not guaranteed. Use it as a helper to find IP addresses — always verify before adding a device.

**Plugins → Samsung TV → Discover Samsung TVs on Network** sends an SSDP multicast across the local network to find Samsung TVs and logs their IP address, name, model, OS, and MAC. It also flags which TVs are already configured in Indigo.

**Requirements for discovery to work:**
- **The TV must be powered fully on.** Standby mode disables SSDP broadcasts on most Samsung models.
- The TV and the Indigo server must be on the **same network segment**. Discovery does not cross router boundaries or VLANs.
- Allow approximately 4 seconds for the scan to complete.

Discovery does not add devices automatically — use the found IP address to manually create a new Samsung TV device in Indigo.

---

### Plugin Scripting

You can control Samsung TVs from Indigo's built-in scripting shell (**Plugins → Open Scripting Shell**) or from any Indigo Python script/schedule.

#### Get a reference to the plugin and a TV device

```python
plugin = indigo.server.getPlugin("com.ryanbuckner.indigoplugin.samsungtv")
tv     = indigo.devices["Living Room TV"]   # use your device name
```

#### Read device states

```python
print(tv.onState)                    # True / False
print(tv.states["volumeLevel"])      # 0–100
print(tv.states["currentApp"])       # e.g. "Netflix"
print(tv.states["currentSource"])    # e.g. "HDMI 1"
print(tv.states["macaddress"])
print(tv.states["model"])
```

#### Power on / off

```python
# Turn on (Wake-on-LAN)
plugin.executeAction("turnOnTV", deviceId=tv.id)

# Turn off
plugin.executeAction("turnOffTV", deviceId=tv.id)

# Toggle
plugin.executeAction("toggleTVPower", deviceId=tv.id)
```

#### Volume

```python
# Step up or down by 1
plugin.executeAction("volumeUp",   deviceId=tv.id)
plugin.executeAction("volumeDown", deviceId=tv.id)

# Set an absolute level (0–100)
plugin.executeAction("setVolume", deviceId=tv.id,
                     props={"volumeLevel": "25"})
```

#### Send a remote button

```python
# Pick any value from the sendCommand action dropdown
plugin.executeAction("sendCommand", deviceId=tv.id,
                     props={"commandCode": "mute"})

# Or send any Samsung key code directly
plugin.executeAction("sendKeyCode", deviceId=tv.id,
                     props={"keyCode": "KEY_RED"})
```

#### Switch input source

```python
plugin.executeAction("selectSource", deviceId=tv.id,
                     props={"sourceId": "HDMI 1"})
# Valid values: "TV", "HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "AV", "Component"
```

#### Launch an app

```python
plugin.executeAction("launchApp", deviceId=tv.id,
                     props={"appName": "Netflix"})
# Valid values: Netflix, YouTube TV, YouTube, Prime Video, Disney+,
#               Hulu, Spotify, Plex, Apple TV, HBO Max, Peacock,
#               Paramount+, ESPN, Tubi, Crunchyroll, TV Plus, Samsung Internet
```

#### Force a state refresh

```python
plugin.executeAction("refresh_tv_data", deviceId=tv.id)
```

#### Example: movie-night scene

```python
plugin = indigo.server.getPlugin("com.ryanbuckner.indigoplugin.samsungtv")
tv     = indigo.devices["Living Room TV"]

if not tv.onState:
    plugin.executeAction("turnOnTV", deviceId=tv.id)
    indigo.server.sleep(8)              # wait for TV to boot

plugin.executeAction("selectSource", deviceId=tv.id, props={"sourceId": "HDMI 1"})
plugin.executeAction("setVolume",    deviceId=tv.id, props={"volumeLevel": "20"})
```

---

### Troubleshooting

- The plugin polls over the local network. The TV must be reachable from the Indigo server.
- When a TV is unreachable it shows a red error badge in the Indigo device list and clears it when connectivity is restored.
- Wake-on-LAN requires the MAC address to be stored. Turn the TV on at least once after adding it so the plugin can auto-discover and persist the MAC address.
- Enable debug logging via **Plugins → Samsung TV → Toggle Debug** to see connection details in the Indigo log.
- Enable detailed state logging via **Plugins → Samsung TV → Toggle Detailed State Logging** to see every device state value on each poll.

More information on the [GitHub Wiki](https://github.com/ryanbuckner/samsungtv-plugin/wiki).
