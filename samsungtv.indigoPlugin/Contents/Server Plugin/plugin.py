#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2023 ryanbuckner
# https://github.com/ryanbuckner/samsungtv-plugin/wiki

import indigo
import sys
import datetime
import samsungtvws
import wakeonlan
import os
import urllib.request
import xml.etree.ElementTree as ET
import socket

sys.path.append('../')

# Maps display names shown in the selectSource action to Samsung key codes.
SOURCE_MAP = {
	"TV":          "KEY_TV",
	"HDMI 1":      "KEY_HDMI1",
	"HDMI 2":      "KEY_HDMI2",
	"HDMI 3":      "KEY_HDMI3",
	"HDMI 4":      "KEY_HDMI4",
	"AV":          "KEY_AV1",
	"Component":   "KEY_COMPONENT1",
}

# Well-known app IDs for current-app detection via the Samsung REST API.
# Order matters: more-specific apps must come before generic ones that share a brand
# (e.g. YouTube TV before YouTube) so the first visible match wins correctly.
COMMON_APPS = {
	"Netflix":           "11101200001",
	"YouTube TV":        "3202103023232",
	"YouTube":           "111299001912",
	"Prime Video":       "3201512006785",
	"Disney+":           "3201901017640",
	"Hulu":              "3201601007625",
	"Spotify":           "3201606009684",
	"Plex":              "3201512006963",
	"Apple TV":          "3201807016597",
	"HBO Max":           "3201601007230",
	"Peacock":           "3202101017621",
	"Paramount+":        "3202005021137",
	"ESPN":              "3201601007235",
	"Tubi":              "3202301028598",
	"Crunchyroll":       "3201803016036",
	"TV Plus":           "3202203026841",
	"Samsung Internet":  "org.tizen.browser",
}

# UPnP RenderingControl port used by Samsung Tizen TVs.
UPNP_PORT = 9197


class Plugin(indigo.PluginBase):

	# ---------------------------------------------------------------------------
	# Init — instance variables only; no Indigo DB access, no network calls
	# ---------------------------------------------------------------------------
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.debug = pluginPrefs.get("showDebugInfo", False)
		self.detailedDebug = pluginPrefs.get("showDetailedDebugInfo", False)
		# dict keyed by device.id → {"retryCount": int, "lastUpdate": str|None}
		self.deviceList = {}
		self.pluginId = pluginId
		self.pluginprefDirectory = '{}/Preferences/Plugins/com.ryanbuckner.indigoplugin.samsungtv'.format(
			indigo.server.getInstallFolderPath()
		)
		self.path = self.pluginprefDirectory + "/"
		self.timeout = 3.0
		self.refresh_frequency = int(pluginPrefs.get('refresh_frequency', 30))

	# ---------------------------------------------------------------------------
	# Lifecycle
	# ---------------------------------------------------------------------------
	def startup(self):
		self.logger.info("")
		self.logger.info("{0:=^130}".format(" Starting Samsung TV Plugin "))
		self.logger.info("{0:<30} {1}".format("Plugin name:",       self.pluginDisplayName))
		self.logger.info("{0:<30} {1}".format("Plugin version:",    self.pluginVersion))
		self.logger.info("{0:<30} {1}".format("Plugin ID:",         self.pluginId))
		self.logger.info("{0:<30} {1}".format("Token path:",        self.path))
		self.logger.info("{0:<30} {1}".format("Refresh frequency:", str(self.refresh_frequency) + " seconds"))
		self.logger.info("{0:<30} {1}".format("Indigo version:",    indigo.server.version))
		self.logger.info("{0:<30} {1}".format("Python version:",    sys.version.replace('\n', '')))
		self.logger.info("{0:<30} {1}".format("Python directory:",  sys.prefix.replace('\n', '')))
		self.logger.info("{0:=^130}".format(""))

		try:
			if not os.path.exists(self.pluginprefDirectory):
				os.makedirs(self.pluginprefDirectory)
		except Exception:
			self.logger.error("Error accessing or creating tokens folder")

	def shutdown(self):
		self.logger.info("Samsung TV Plugin shutting down")
		for devId in list(self.deviceList.keys()):
			if devId in indigo.devices:
				dev = indigo.devices[devId]
				try:
					dev.updateStateOnServer("status", "offline")
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
				except Exception:
					pass

	def prepareToSleep(self):
		self.logger.debug("Preparing for sleep — TV WebSocket connections will be dropped")

	def wakeUp(self):
		self.logger.debug("Waking from sleep — refreshing all TV states")
		for devId in list(self.deviceList.keys()):
			if devId in indigo.devices:
				try:
					self.updatedevicestates(indigo.devices[devId])
				except Exception:
					self.logger.exception(f"Error refreshing device {devId} after wake")

	# ---------------------------------------------------------------------------
	# Device lifecycle
	# ---------------------------------------------------------------------------
	def deviceStartComm(self, device):
		super().deviceStartComm(device)
		self.logger.debug("Starting device: " + device.name)
		device.stateListOrDisplayStateIdChanged()

		if device.deviceTypeId == "televisions":
			self.deviceList[device.id] = {"retryCount": 0, "lastUpdate": None}
			self.updatedevicestates(device)

	def deviceStopComm(self, device):
		super().deviceStopComm(device)
		self.logger.debug("Stopping device: " + device.name)
		self.deviceList.pop(device.id, None)

	# ---------------------------------------------------------------------------
	# Concurrent polling thread
	# ---------------------------------------------------------------------------
	def runConcurrentThread(self):
		self.logger.debug(f"Concurrent thread started — polling every {self.refresh_frequency}s")
		try:
			while True:
				self.sleep(self.refresh_frequency)
				for deviceId in list(self.deviceList.keys()):
					if deviceId in indigo.devices:
						try:
							self.updatedevicestates(indigo.devices[deviceId])
						except Exception:
							self.logger.exception(f"Error updating device {deviceId}")
		except self.StopThread:
			pass

	# ---------------------------------------------------------------------------
	# Validation
	# ---------------------------------------------------------------------------
	def validateDeviceConfigUi(self, valuesDict, typeId, device):
		if typeId == 'televisions':
			if not valuesDict.get('ipaddress', '').strip():
				errorsDict = indigo.Dict()
				errorsDict['ipaddress'] = "IP Address is a required field"
				return (False, valuesDict, errorsDict)

			if not valuesDict.get('token', '').strip():
				errorsDict = indigo.Dict()
				errorsDict['token'] = "Token file name is required"
				return (False, valuesDict, errorsDict)

			if not valuesDict.get('port', '').strip():
				errorsDict = indigo.Dict()
				errorsDict['port'] = "Port is a required field"
				return (False, valuesDict, errorsDict)

		valuesDict['address'] = valuesDict['ipaddress']
		file_path = self.path + valuesDict['token']
		if not os.path.exists(file_path):
			self.logger.debug("Token at " + file_path + " does not exist, creating")
			try:
				with open(file_path, 'w'):
					pass
			except Exception as e:
				self.logger.debug(f"Error creating token file: {e}")

		return (True, valuesDict)

	def validatePrefsConfigUi(self, valuesDict):
		try:
			freq = int(valuesDict.get('refresh_frequency', 30))
			if freq < 10:
				errorsDict = indigo.Dict()
				errorsDict['refresh_frequency'] = "Minimum refresh frequency is 10 seconds"
				return (False, valuesDict, errorsDict)
			self.refresh_frequency = freq
		except ValueError:
			errorsDict = indigo.Dict()
			errorsDict['refresh_frequency'] = "Must be a whole number"
			return (False, valuesDict, errorsDict)
		return (True, valuesDict)

	def closedPrefsConfigUi(self, valuesDict, userCancelled):
		if not userCancelled:
			self.refresh_frequency = int(valuesDict.get('refresh_frequency', 30))

	def menuChanged(self, valuesDict=None, typeId=None, devId=None):
		return valuesDict

	def menu_callback(self, valuesDict, *args, **kwargs):
		self.logger.debug(f"Menu callback: {valuesDict}")
		return valuesDict

	# ---------------------------------------------------------------------------
	# Debug toggles (Plugins menu)
	# ---------------------------------------------------------------------------
	def toggleDebugging(self):
		if self.debug:
			self.debug = False
			self.pluginPrefs['showDebugInfo'] = False
			self.logger.info("Debug logging turned off")
		else:
			self.debug = True
			self.pluginPrefs['showDebugInfo'] = True
			self.logger.debug("Debug logging turned on")

	def toggleDetailedDebugging(self):
		if self.detailedDebug:
			self.detailedDebug = False
			self.pluginPrefs['showDetailedDebugInfo'] = False
			self.logger.info("Detailed state logging turned off")
		else:
			self.detailedDebug = True
			self.pluginPrefs['showDetailedDebugInfo'] = True
			self.logger.info("Detailed state logging turned on")

	def listDeviceInfo(self):
		devices = [d for d in indigo.devices.iter("self") if d.deviceTypeId == "televisions"]
		if not devices:
			self.logger.info("No Samsung TV devices are configured")
			return
		self.logger.info("{0:=^130}".format(" Samsung TV Device Info "))
		for dev in devices:
			self.logger.info("{0:<20} {1}".format("Device name:",  dev.name))
			self.logger.info("{0:<20} {1}".format("IP address:",   dev.pluginProps.get('ipaddress', 'not set')))
			self.logger.info("{0:<20} {1}".format("MAC address:",  dev.pluginProps.get('macaddress', '') or dev.states.get('macaddress', 'not yet discovered')))
			self.logger.info("{0:<20} {1}".format("OS:",           dev.states.get('os', 'unknown')))
			self.logger.info("{0:-^130}".format(""))
		self.logger.info("{0:=^130}".format(""))

	def discoverSamsungTVs(self):
		SSDP_ADDR = "239.255.255.255"
		SSDP_PORT = 1900
		SSDP_MX   = 3
		ST        = "urn:samsung.com:device:RemoteControlReceiver:1"

		msg = "\r\n".join([
			"M-SEARCH * HTTP/1.1",
			f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
			'MAN: "ssdp:discover"',
			f"MX: {SSDP_MX}",
			f"ST: {ST}",
			"", ""
		])

		self.logger.info("Scanning network for Samsung TVs — this takes about 4 seconds...")

		found = {}
		try:
			sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
			sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
			sock.settimeout(SSDP_MX + 1)
			sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
			while True:
				try:
					_, addr = sock.recvfrom(2048)
					found[addr[0]] = True
				except socket.timeout:
					break
		except Exception as exc:
			self.logger.error(f"SSDP discovery failed: {exc}")
			return
		finally:
			try:
				sock.close()
			except Exception:
				pass

		if not found:
			self.logger.info("No Samsung TVs found. Make sure TVs are powered on and on the same network segment.")
			return

		configured_ips = {
			d.pluginProps.get('ipaddress', '')
			for d in indigo.devices.iter("self")
			if d.deviceTypeId == "televisions"
		}

		self.logger.info("{0:=^130}".format(f" Discovered {len(found)} Samsung TV(s) "))
		for ip in found:
			status = "(already configured)" if ip in configured_ips else "(not yet configured)"
			self.logger.info(f"  IP Address:  {ip}  {status}")
			try:
				tv   = samsungtvws.SamsungTVWS(host=ip, port=8002, token_file=self.path + "discovery-temp.txt", timeout=2.0)
				info = tv.rest_device_info().get('device', {})
				self.logger.info(f"  Name:        {info.get('name', 'unknown')}")
				self.logger.info(f"  Model:       {info.get('modelName', 'unknown')}")
				self.logger.info(f"  OS:          {info.get('OS', 'unknown')}")
				self.logger.info(f"  MAC:         {info.get('wifiMac', 'unknown')}")
			except Exception:
				self.logger.info("  (Could not query device details — TV may need to be woken first)")
			self.logger.info("{0:-^130}".format(""))
		self.logger.info("{0:=^130}".format(""))

	# ---------------------------------------------------------------------------
	# TV connection helper
	# ---------------------------------------------------------------------------
	def _getTV(self, device):
		ipaddress  = device.pluginProps['ipaddress']
		token_file = self.path + device.pluginProps['token']
		try:
			return samsungtvws.SamsungTVWS(
				host=ipaddress, port=8002,
				token_file=token_file, timeout=self.timeout
			)
		except Exception:
			self.logger.debug(f"Failed to create TV connection for {device.name}")
			return None

	# ---------------------------------------------------------------------------
	# State polling
	# ---------------------------------------------------------------------------
	def updatedevicestates(self, device):
		ipaddress  = device.pluginProps['ipaddress']
		token_file = self.path + device.pluginProps['token']

		self.logger.debug(f"Polling {device.name} ({device.id})")

		try:
			tv   = samsungtvws.SamsungTVWS(host=ipaddress, port=8002, token_file=token_file, timeout=self.timeout)
			info = tv.rest_device_info()
			tvState = info['device']['PowerState'].lower()
			self.logger.debug(f"Power state of {device.name}: {tvState}")
		except Exception:
			self.logger.debug(f"Cannot reach {device.address} — marking offline")
			device.setErrorStateOnServer("Unreachable")
			device.updateStatesOnServer([
				{'key': 'status',     'value': 'off'},
				{'key': 'onOffState', 'value': False},
			])
			device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
			if device.id in self.deviceList:
				self.deviceList[device.id]['retryCount'] = self.deviceList[device.id].get('retryCount', 0) + 1
			return

		# Clear any previous error badge
		device.setErrorStateOnServer(None)

		# Persist MAC into pluginProps so WOL works even before first successful poll
		mac = info['device'].get('wifiMac', '')
		if mac and device.pluginProps.get('macaddress', '') != mac:
			props = device.pluginProps
			props['macaddress'] = mac
			device.replacePluginPropsOnServer(props)

		# Detect currently running app and read volume — only meaningful when the TV is on
		current_app = ""
		if tvState == 'on':
			current_app = self._getCurrentApp(tv)

		volume = self._getVolumeUPnP(ipaddress) if tvState == 'on' else None
		if volume is not None:
			self.logger.debug(f"Volume for {device.name}: {volume}")

		device_states = [
			{'key': 'status',          'value': tvState},
			{'key': 'onOffState',      'value': tvState == 'on'},
			{'key': 'name',            'value': info['device'].get('name', '')},
			{'key': 'os',              'value': info['device'].get('OS', '')},
			{'key': 'macaddress',      'value': mac},
			{'key': 'device_id',       'value': info['device'].get('id', '')},
			{'key': 'model',           'value': info['device'].get('modelName', '')},
			{'key': 'tokenauthsupport','value': info['device'].get('TokenAuthSupport', '')},
			{'key': 'voicesupport',    'value': info['device'].get('VoiceSupport', '')},
			{'key': 'firmwareversion', 'value': info['device'].get('firmwareVersion', '')},
			{'key': 'networktype',     'value': info['device'].get('networkType', '')},
			{'key': 'type',            'value': info.get('type', '')},
			{'key': 'currentApp',      'value': current_app},
		]
		if volume is not None:
			device_states.append({'key': 'volumeLevel', 'value': volume})

		if self.detailedDebug:
			self.logger.debug(f"--- States for {device.name} ---")
			for state in device_states:
				self.logger.debug(f"  {state['key']:<20} = {state['value']}")
			self.logger.debug(f"--- End states for {device.name} ---")

		device.updateStatesOnServer(device_states)
		device.updateStateImageOnServer(
			indigo.kStateImageSel.SensorOn if tvState == 'on' else indigo.kStateImageSel.SensorOff
		)

		if device.id in self.deviceList:
			self.deviceList[device.id]['lastUpdate'] = str(datetime.datetime.now())
			self.deviceList[device.id]['retryCount'] = 0

	def _getCurrentApp(self, tv):
		"""Poll the TV for a running app from COMMON_APPS. Returns app name or empty string."""
		for app_name, app_id in COMMON_APPS.items():
			try:
				status = tv.rest_app_status(app_id)
				if status and (status.get('visible') or status.get('running')):
					return app_name
			except Exception:
				pass
		return ""

	def _getVolumeUPnP(self, ip):
		"""Read current volume from the TV via UPnP RenderingControl (port 9197).
		Returns an int 0-100, or None if the TV does not respond."""
		soap = (
			'<?xml version="1.0" encoding="utf-8"?>'
			'<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"'
			' xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
			'<s:Body>'
			'<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
			'<InstanceID>0</InstanceID><Channel>Master</Channel>'
			'</u:GetVolume>'
			'</s:Body>'
			'</s:Envelope>'
		)
		try:
			req = urllib.request.Request(
				f"http://{ip}:{UPNP_PORT}/upnp/control/RenderingControl1",
				data=soap.encode('utf-8'),
				headers={
					'Content-Type': 'text/xml; charset="utf-8"',
					'SOAPAction': '"urn:schemas-upnp-org:service:RenderingControl:1#GetVolume"',
				},
				method='POST'
			)
			with urllib.request.urlopen(req, timeout=2) as resp:
				root = ET.fromstring(resp.read())
				el = root.find('.//CurrentVolume')
				if el is not None:
					return int(el.text)
		except Exception:
			pass
		return None

	def refresh_tv_data(self, pluginAction, device):
		self.updatedevicestates(device)

	# ---------------------------------------------------------------------------
	# Relay / switch built-in action handler
	# ---------------------------------------------------------------------------
	def actionControlDevice(self, action, dev):
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			send_success = self.turnOnTV(dev)
			if send_success:
				self.logger.info(f"sent \"{dev.name}\" on")
				dev.updateStateOnServer("onOffState", True)
				dev.updateStateOnServer("status", "on")
			else:
				self.logger.error(f"send \"{dev.name}\" on failed")
				self.logger.info(f"Verify the IP address for \"{dev.name}\" ({dev.pluginProps.get('ipaddress', 'unknown')}) has not changed")

		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			send_success = self.turnOffTV(dev)
			if send_success:
				self.logger.info(f"sent \"{dev.name}\" off")
				dev.updateStateOnServer("onOffState", False)
				dev.updateStateOnServer("status", "off")
			else:
				self.logger.error(f"send \"{dev.name}\" off failed")

	# ---------------------------------------------------------------------------
	# Power actions
	# ---------------------------------------------------------------------------
	def toggleTVPower(self, pluginAction, device):
		if device.onState:
			send_success = self.turnOffTV(device)
			if send_success:
				self.logger.info(f"sent \"{device.name}\" off")
				device.updateStateOnServer("onOffState", False)
				device.updateStateOnServer("status", "off")
			else:
				self.logger.error(f"send \"{device.name}\" off failed")
		else:
			send_success = self.turnOnTV(device)
			if send_success:
				self.logger.info(f"sent \"{device.name}\" on")
				device.updateStateOnServer("onOffState", True)
				device.updateStateOnServer("status", "on")
			else:
				self.logger.error(f"send \"{device.name}\" on failed")
				self.logger.info(f"Verify the IP address for \"{device.name}\" ({device.pluginProps.get('ipaddress', 'unknown')}) has not changed")

	def turnOnTVAction(self, pluginAction, device, callerWaitingForResult=None):
		send_success = self.turnOnTV(device)
		if send_success:
			self.logger.info(f"sent \"{device.name}\" on")
			device.updateStateOnServer("onOffState", True)
			device.updateStateOnServer("status", "on")
		else:
			self.logger.error(f"send \"{device.name}\" on failed")
			self.logger.info(f"Verify the IP address for \"{device.name}\" ({device.pluginProps.get('ipaddress', 'unknown')}) has not changed")

	def turnOffTVAction(self, pluginAction, device, callerWaitingForResult=None):
		send_success = self.turnOffTV(device)
		if send_success:
			self.logger.info(f"sent \"{device.name}\" off")
			device.updateStateOnServer("onOffState", False)
			device.updateStateOnServer("status", "off")
		else:
			self.logger.error(f"send \"{device.name}\" off failed")
			self.logger.info(f"Verify the IP address for \"{device.name}\" ({device.pluginProps.get('ipaddress', 'unknown')}) has not changed")

	def volumeUpAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeUp(device)

	def volumeDownAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeDown(device)

	def turnOnTV(self, device):
		macaddress = device.pluginProps.get('macaddress', '') or device.states.get('macaddress', '')
		if not macaddress:
			self.logger.error(f"\"{device.name}\": no MAC address stored — turn the TV on manually, wait for a poll cycle, then try again")
			return False
		if device.states.get('status') == "on":
			self.logger.info(f"\"{device.name}\" is already on")
			return False
		self.logger.info(f"Sending WOL magic packet to {macaddress} for \"{device.name}\"")
		try:
			wakeonlan.send_magic_packet(macaddress)
			return True
		except Exception as m:
			self.logger.error(f"WOL for \"{device.name}\" failed: {m}")
			return False

	def turnOffTV(self, device):
		if device.states.get('status') != "on":
			self.logger.info(f"\"{device.name}\" is not on")
			return False
		self.logger.info(f"Sending power key to \"{device.name}\"")
		try:
			tv = samsungtvws.SamsungTVWS(
				host=device.pluginProps['ipaddress'], port=8002,
				token_file=self.path + device.pluginProps['token'],
				timeout=self.timeout
			)
			tv.shortcuts().power()
			return True
		except Exception as m:
			self.logger.error(f"Power off for \"{device.name}\" failed: {m}")
			return False

	# ---------------------------------------------------------------------------
	# Key send helper — single point for all key dispatch
	# ---------------------------------------------------------------------------
	def _sendKey(self, device, key, label="key"):
		tv = self._getTV(device)
		if not tv:
			self.logger.error(f"Cannot reach \"{device.name}\" to send {label}")
			return False
		try:
			tv.send_key(key)
			return True
		except Exception as m:
			self.logger.error(f"{label} failed on \"{device.name}\": {m}")
			return False

	# ---------------------------------------------------------------------------
	# sendCommand action — remote button dispatch
	# ---------------------------------------------------------------------------
	def sendCommand(self, pluginAction, device, callerWaitingForResult=None):
		cmd = pluginAction.props.get("commandCode", "")
		self.logger.debug(f"sendCommand '{cmd}' → {device.name}")

		dispatch = {
			'mute':         self._mute,
			'volume_up':    self._volumeUp,
			'volume_down':  self._volumeDown,
			'play':         self._play,
			'pause':        self._pause,
			'stop':         self._stop,
			'fast_forward': self._fastForward,
			'rewind':       self._rewind,
			'channel_up':   self._channelUp,
			'channel_down': self._channelDown,
			'up':           self._up,
			'down':         self._down,
			'left':         self._left,
			'right':        self._right,
			'enter':        self._enter,
			'back':         self._back,
			'home':         self._home,
			'exit':         self._exit,
			'menu':         self._menu,
			'source':       self._source,
			'guide':        self._guide,
			'tools':        self._tools,
			'info':         self._info,
		}

		fn = dispatch.get(cmd)
		if fn:
			fn(device)
		else:
			self.logger.error(f"Unknown command code: '{cmd}'")

	# ---------------------------------------------------------------------------
	# sendKeyCode action — arbitrary Samsung key code
	# ---------------------------------------------------------------------------
	def sendKeyCode(self, pluginAction, device, callerWaitingForResult=None):
		key = pluginAction.props.get("keyCode", "").strip()
		if not key:
			self.logger.error("sendKeyCode: no key code specified")
			return
		self.logger.debug(f"sendKeyCode '{key}' → {device.name}")
		self._sendKey(device, key, key)

	# ---------------------------------------------------------------------------
	# setVolume action — steps to an absolute level (0–100)
	# ---------------------------------------------------------------------------
	def setVolume(self, pluginAction, device, callerWaitingForResult=None):
		try:
			target = max(0, min(100, int(pluginAction.props.get("volumeLevel", 50))))
		except (ValueError, TypeError):
			self.logger.error("setVolume: invalid volume level")
			return

		current = device.states.get("volumeLevel", 50)
		if not isinstance(current, int):
			current = 50

		diff = target - current
		if diff == 0:
			return

		tv = self._getTV(device)
		if not tv:
			return

		self.logger.debug(f"Volume {current} → {target} ({diff:+d} steps) on \"{device.name}\"")
		try:
			key  = "KEY_VOLUP" if diff > 0 else "KEY_VOLDOWN"
			for _ in range(abs(diff)):
				tv.send_key(key)
			device.updateStateOnServer("volumeLevel", target)
		except Exception as m:
			self.logger.error(f"setVolume failed on \"{device.name}\": {m}")

	# ---------------------------------------------------------------------------
	# selectSource action — switch input via Samsung key code
	# ---------------------------------------------------------------------------
	def selectSource(self, pluginAction, device, callerWaitingForResult=None):
		source_id = pluginAction.props.get("sourceId", "")
		key = SOURCE_MAP.get(source_id)
		if not key:
			self.logger.error(f"selectSource: unknown source '{source_id}'")
			return
		self.logger.debug(f"Selecting source '{source_id}' ({key}) on \"{device.name}\"")
		if self._sendKey(device, key, f"source:{source_id}"):
			device.updateStateOnServer("currentSource", source_id)

	def getSourceList(self, filter="", valuesDict=None, typeId="", targetId=0):
		"""Dynamic list callback for the selectSource action ConfigUI."""
		return [(name, name) for name in SOURCE_MAP.keys()]

	# ---------------------------------------------------------------------------
	# launchApp action — open an app by name via the Samsung REST API
	# ---------------------------------------------------------------------------
	def launchApp(self, pluginAction, device, callerWaitingForResult=None):
		app_name = pluginAction.props.get("appName", "")
		app_id = COMMON_APPS.get(app_name)
		if not app_id:
			self.logger.error(f"launchApp: unknown app '{app_name}'")
			return
		self.logger.info(f"Launching {app_name} on \"{device.name}\"")
		tv = self._getTV(device)
		if not tv:
			return
		try:
			tv.rest_app_run(app_id)
			device.updateStateOnServer("currentApp", app_name)
		except Exception as m:
			self.logger.error(f"Failed to launch {app_name} on \"{device.name}\": {m}")

	def getAppList(self, filter="", valuesDict=None, typeId="", targetId=0):
		"""Dynamic list callback for the launchApp action ConfigUI."""
		return [(name, name) for name in COMMON_APPS.keys()]

	# ---------------------------------------------------------------------------
	# Individual key methods (called by sendCommand dispatch)
	# ---------------------------------------------------------------------------
	def _mute(self, device):
		if self._sendKey(device, "KEY_MUTE", "Mute"):
			device.updateStateOnServer("isMuted", not device.states.get("isMuted", False))

	def _volumeUp(self, device):
		if self._sendKey(device, "KEY_VOLUP", "Volume Up"):
			current = device.states.get("volumeLevel", 50)
			device.updateStateOnServer("volumeLevel", min(100, int(current) + 1))

	def _volumeDown(self, device):
		if self._sendKey(device, "KEY_VOLDOWN", "Volume Down"):
			current = device.states.get("volumeLevel", 50)
			device.updateStateOnServer("volumeLevel", max(0, int(current) - 1))

	def _play(self, device):         self._sendKey(device, "KEY_PLAY",      "Play")
	def _pause(self, device):        self._sendKey(device, "KEY_PAUSE",     "Pause")
	def _stop(self, device):         self._sendKey(device, "KEY_STOP",      "Stop")
	def _fastForward(self, device):  self._sendKey(device, "KEY_FF",        "Fast Forward")
	def _rewind(self, device):       self._sendKey(device, "KEY_REWIND",    "Rewind")
	def _channelUp(self, device):    self._sendKey(device, "KEY_CHUP",      "Channel Up")
	def _channelDown(self, device):  self._sendKey(device, "KEY_CHDOWN",    "Channel Down")
	def _up(self, device):           self._sendKey(device, "KEY_UP",        "Up")
	def _down(self, device):         self._sendKey(device, "KEY_DOWN",      "Down")
	def _left(self, device):         self._sendKey(device, "KEY_LEFT",      "Left")
	def _right(self, device):        self._sendKey(device, "KEY_RIGHT",     "Right")
	def _enter(self, device):        self._sendKey(device, "KEY_ENTER",     "Enter")
	def _back(self, device):         self._sendKey(device, "KEY_RETURN",    "Back")
	def _home(self, device):         self._sendKey(device, "KEY_HOME",      "Home")
	def _exit(self, device):         self._sendKey(device, "KEY_EXIT",      "Exit")
	def _menu(self, device):         self._sendKey(device, "KEY_MENU",      "Menu")
	def _source(self, device):       self._sendKey(device, "KEY_SOURCE",    "Source")
	def _guide(self, device):        self._sendKey(device, "KEY_GUIDE",     "Guide")
	def _tools(self, device):        self._sendKey(device, "KEY_TOOLS",     "Tools")
	def _info(self, device):         self._sendKey(device, "KEY_INFO",      "Info")
