#!/usr/bin/env python3
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2023 ryanbuckner
# https://github.com/ryanbuckner/samsungtv-plugin/wiki

try:
	# This is primarily for IDEs — the indigo module is always injected into
	# the global namespace by the host process when the plugin actually runs.
	import indigo
except ImportError:
	pass

import sys
import datetime
import samsungtvws
import wakeonlan
import os
import urllib.request
import xml.etree.ElementTree as ET
import socket
import ipaddress as ipaddress_module
import threading
import time

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
		level = pluginPrefs.get("debugLevel", "off")
		self.debug         = level in ("debug", "detailed")
		self.detailedDebug = level == "detailed"
		# dict keyed by device.id → {"retryCount": int, "lastUpdate": str|None}
		self.deviceList = {}
		self.pluginId = pluginId
		self.pluginprefDirectory = '{}/Preferences/Plugins/com.ryanbuckner.indigoplugin.samsungtv'.format(
			indigo.server.getInstallFolderPath()
		)
		self.path = self.pluginprefDirectory + "/"
		self.timeout = 3.0
		try:
			self.refresh_frequency = int(pluginPrefs.get('refresh_frequency', 30))
		except (TypeError, ValueError):
			# A bad/blank stored value must not raise here: an exception in
			# __init__ is unrecoverable (Indigo marks the plugin fatally
			# suspended), where falling back and fixing it in Preferences later
			# is simply an inconvenience.
			self.refresh_frequency = 30
			self.logger.debug(f"Invalid stored refresh_frequency {pluginPrefs.get('refresh_frequency')!r} — defaulting to 30s")

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
					dev.updateStateOnServer("status", "off")
					dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
				except Exception:
					pass
				self._closeTV(dev)

	def prepareToSleep(self):
		self.logger.debug("Preparing for sleep — closing cached TV connections")
		for devId in list(self.deviceList.keys()):
			if devId in indigo.devices:
				self._closeTV(indigo.devices[devId])

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
			# Merge into any existing entry instead of replacing it outright: if
			# deviceStartComm() is ever re-entered while a previous poll is still
			# running (and holding this entry's lock), replacing the dict would
			# hand the new poll a fresh, uncontended lock — letting two polls
			# race against the same physical TV at once.
			entry = self.deviceList.setdefault(device.id, {})
			entry["retryCount"] = 0
			entry["lastUpdate"] = None
			self._ensureTokenFile(device)
			# Poll in the background instead of inline: Indigo calls deviceStartComm()
			# serially for every device at launch, so a slow/offline TV here would
			# block every other device (and the plugin) from starting promptly.
			# Stagger each device's first connection instead of firing them all
			# at once: several brand-new connections to different LAN hosts in
			# the same instant, repeated on every reload, can look like
			# port-scanning to a router/mesh AP's security heuristics and
			# trigger a temporary block on the source — spacing them out avoids
			# that pattern entirely.
			self._startupPollCount = getattr(self, '_startupPollCount', 0) + 1
			delay = min((self._startupPollCount - 1) * 1.5, 10.0)
			self._backgroundPoll(device, delay=delay)

	def _ensureTokenFile(self, device):
		"""Create the device's token file if it doesn't exist yet. Done here
		(not in validateDeviceConfigUi) so cancelling the config dialog can't
		leave an orphaned token file on disk as a side effect of validation."""
		token_path = self._getTokenPath(device)
		if not os.path.exists(token_path):
			self.logger.debug(f"Token file at {token_path} does not exist, creating")
			try:
				with open(token_path, 'w'):
					pass
			except Exception as e:
				# Visible at the default log level: a failed token file means
				# samsungtvws has nowhere to persist the TV's auth token, so the
				# device will never work until this is fixed.
				self.logger.error(f"Could not create token file for \"{device.name}\" at {token_path}: {e}")

	def _backgroundPoll(self, device, delay=0):
		threading.Thread(target=self._safePoll, args=(device, delay), daemon=True).start()

	def _safePoll(self, device, delay=0):
		try:
			if delay:
				time.sleep(delay)
			self.updatedevicestates(device)
		except Exception:
			self.logger.exception(f"Error during background poll of {device.name}")

	def deviceStopComm(self, device):
		super().deviceStopComm(device)
		self.logger.debug("Stopping device: " + device.name)
		self._closeTV(device)
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
	def validateDeviceConfigUi(self, valuesDict, typeId, devId):
		# Note: devId is a plain int (not a Device object) per the Indigo SDK —
		# it has no .name/.id attributes, and for a brand-new device being
		# created for the first time it may not exist in indigo.devices yet.
		if typeId == 'televisions':
			ip_str = valuesDict.get('ipaddress', '').strip()
			if not ip_str:
				errorsDict = indigo.Dict()
				errorsDict['ipaddress'] = "IP Address is a required field"
				return (False, valuesDict, errorsDict)
			try:
				ipaddress_module.ip_address(ip_str)
			except ValueError:
				self.logger.error(f"Samsung TV device config: \"{ip_str}\" is not a valid IP address")
				errorsDict = indigo.Dict()
				errorsDict['ipaddress'] = "Enter a valid IP address (e.g. 192.168.1.50)"
				return (False, valuesDict, errorsDict)

			port_str = valuesDict.get('port', '').strip()
			if not port_str:
				errorsDict = indigo.Dict()
				errorsDict['port'] = "Port is a required field"
				return (False, valuesDict, errorsDict)
			try:
				if not (1 <= int(port_str) <= 65535):
					raise ValueError
			except ValueError:
				self.logger.error(f"Samsung TV device config: \"{port_str}\" is not a valid port (must be 1-65535)")
				errorsDict = indigo.Dict()
				errorsDict['port'] = "Port must be a number between 1 and 65535"
				return (False, valuesDict, errorsDict)

			valuesDict['address'] = ip_str

			# Sanitize the token file name: strip any path component so a stray
			# separator (or "..") can't make samsungtvws read/write outside the
			# plugin's own prefs folder. Default it per-device, keyed off the IP,
			# so two TVs created with the stock default don't silently share (and
			# clobber) the same token file.
			token_name = os.path.basename(valuesDict.get('token', '').strip())
			if not token_name:
				token_name = f"tv-token-{ip_str.replace('.', '-')}.txt"
			valuesDict['token'] = token_name

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
			self._applyDebugLevel(valuesDict.get('debugLevel', 'off'))

	def menuChanged(self, valuesDict=None, typeId=None, devId=None):
		return valuesDict

	def menu_callback(self, valuesDict, *args, **kwargs):
		self.logger.debug(f"Menu callback: {valuesDict}")
		return valuesDict

	# ---------------------------------------------------------------------------
	# Debug helpers
	# ---------------------------------------------------------------------------
	def _applyDebugLevel(self, level):
		self.debug         = level in ("debug", "detailed")
		self.detailedDebug = level == "detailed"
		self.pluginPrefs["debugLevel"] = level

	# ---------------------------------------------------------------------------
	# Debug toggles (Plugins menu)
	# ---------------------------------------------------------------------------
	def toggleDebugging(self):
		current = self.pluginPrefs.get("debugLevel", "off")
		if current == "off":
			self._applyDebugLevel("debug")
			self.logger.info("Debug logging turned on")
		else:
			self._applyDebugLevel("off")
			self.logger.info("Debug logging turned off")

	def toggleDetailedDebugging(self):
		current = self.pluginPrefs.get("debugLevel", "off")
		if current == "detailed":
			self._applyDebugLevel("debug")
			self.logger.info("Detailed state logging turned off (debug logging still on)")
		else:
			self._applyDebugLevel("detailed")
			self.logger.info("Detailed state logging turned on — polling all TVs now...")
			for devId in list(self.deviceList.keys()):
				if devId in indigo.devices:
					try:
						self.updatedevicestates(indigo.devices[devId])
					except Exception:
						self.logger.exception(f"Error polling device {devId}")

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
		SSDP_MX   = 4

		# Multiple STs maximize coverage across Samsung firmware versions.
		# ssdp:all catches everything; the narrow ST catches older models that only respond to it.
		search_targets = [
			"ssdp:all",
			"upnp:rootdevice",
			"urn:samsung.com:device:RemoteControlReceiver:1",
		]

		self.logger.info("Scanning network for Samsung TVs — this takes about 6 seconds...")

		candidate_ips = set()

		for st in search_targets:
			msg = "\r\n".join([
				"M-SEARCH * HTTP/1.1",
				f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
				'MAN: "ssdp:discover"',
				f"MX: {SSDP_MX}",
				f"ST: {st}",
				"", ""
			])
			try:
				sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
				sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
				sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
				sock.settimeout(SSDP_MX + 1)

				# Bind to the local interface so multicast egresses on the right NIC (important on macOS)
				try:
					local_ip = socket.gethostbyname(socket.gethostname())
					sock.bind((local_ip, 0))
				except Exception:
					pass

				sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))

				while True:
					try:
						data, addr = sock.recvfrom(4096)
						response = data.decode('utf-8', errors='ignore').lower()
						if 'samsung' in response:
							candidate_ips.add(addr[0])
							self.logger.debug(f"SSDP: samsung device at {addr[0]} (ST={st})")
					except socket.timeout:
						break
			except Exception as exc:
				self.logger.debug(f"SSDP search for '{st}' failed: {exc}")
			finally:
				try:
					sock.close()
				except Exception:
					pass

		if not candidate_ips:
			self.logger.info("No Samsung TVs found. Make sure TVs are powered on and on the same network segment.")
			return

		# Confirm each candidate is actually a Samsung TV by querying its REST API.
		# rest_device_info() is unauthenticated, so the plain REST client is used
		# directly here — no token file needed (and none left behind on disk).
		found = {}
		for ip in candidate_ips:
			try:
				rest = samsungtvws.rest.SamsungTVRest(host=ip, port=8002, timeout=2.0)
				info = rest.rest_device_info().get('device', {})
				found[ip] = info
				self.logger.debug(f"Validated Samsung TV at {ip}: {info.get('modelName', 'unknown')}")
			except Exception:
				self.logger.debug(f"Could not validate {ip} via REST API — skipping")

		if not found:
			self.logger.info("No Samsung TVs found. Make sure TVs are powered on and on the same network segment.")
			return

		configured_ips = {
			d.pluginProps.get('ipaddress', '')
			for d in indigo.devices.iter("self")
			if d.deviceTypeId == "televisions"
		}

		self.logger.info("{0:=^130}".format(f" Discovered {len(found)} Samsung TV(s) "))
		for ip, info in found.items():
			status = "(already configured)" if ip in configured_ips else "(not yet configured)"
			self.logger.info(f"  IP Address:  {ip}  {status}")
			self.logger.info(f"  Name:        {info.get('name', 'unknown')}")
			self.logger.info(f"  Model:       {info.get('modelName', 'unknown')}")
			self.logger.info(f"  OS:          {info.get('OS', 'unknown')}")
			self.logger.info(f"  MAC:         {info.get('wifiMac', 'unknown')}")
			self.logger.info("{0:-^130}".format(""))
		self.logger.info("{0:=^130}".format(""))

	def _getPort(self, device):
		"""Returns the configured port for a device, falling back to 8002 (the
		default for Tizen TVs with token-based TLS auth) if unset or invalid."""
		try:
			port = int(device.pluginProps.get('port', 8002) or 8002)
			return port if 1 <= port <= 65535 else 8002
		except (TypeError, ValueError):
			return 8002

	def _describeException(self, e):
		"""Returns (detail_string, root_cause_exception) for an exception that
		may have been re-wrapped by samsungtvws. The library catches
		requests.ConnectionError internally and re-raises its own generic
		HttpApiError ("TV unreachable or feature not supported on this
		model."), discarding the real error's type and text. The original is
		still chained via `raise ... from err`, so __cause__ (and note
		requests.SSLError IS a ConnectionError subclass, so an SSL failure
		gets swallowed into that same generic message) is where the real
		detail lives — walk it to find the innermost cause."""
		root = e
		seen = set()
		while getattr(root, '__cause__', None) is not None and id(root) not in seen:
			seen.add(id(root))
			root = root.__cause__
		detail = f"{type(e).__name__}: {e}"
		if root is not e:
			detail += f" (caused by {type(root).__name__}: {root})"
		return detail, root

	def _checkTcpPort(self, ip, port, timeout=3.0):
		"""Bare TCP connect check — bypasses HTTP/TLS/samsungtvws entirely, to
		isolate whether the host is even accepting connections on this port
		at all from whatever happens at the Tizen API layer above it."""
		start = time.monotonic()
		try:
			with socket.create_connection((ip, port), timeout=timeout):
				return True, f"connected in {time.monotonic() - start:.2f}s"
		except Exception as e:
			return False, f"{type(e).__name__}: {e}"

	def _getTokenPath(self, device):
		"""Returns the full path to a device's token file, re-sanitizing the
		stored token name every time (not just at ConfigUI validation time).
		pluginProps can be set by things that bypass validateDeviceConfigUi
		entirely — a script calling replacePluginPropsOnServer(), the Indigo
		HTTP/WS API, device import/export — so trusting it blindly here would
		let a value like "../../../Library/something" read/write files outside
		the plugin's own prefs folder."""
		token_name = os.path.basename(device.pluginProps.get('token', '').strip())
		if not token_name:
			ip_str = device.pluginProps.get('ipaddress', '').strip() or "unknown"
			token_name = f"tv-token-{ip_str.replace('.', '-')}.txt"
		return self.path + token_name

	# ---------------------------------------------------------------------------
	# TV connection helper — caches one SamsungTVWS object per device so repeated
	# key sends reuse the same underlying WebSocket instead of paying a fresh
	# handshake (and possible re-auth prompt on the TV) for every single button.
	# ---------------------------------------------------------------------------
	def _getTV(self, device, forceNew=False):
		entry = self.deviceList.setdefault(device.id, {})
		if forceNew:
			self._closeTV(device)
		if entry.get('tv') is None:
			ipaddress  = device.pluginProps['ipaddress']
			token_file = self._getTokenPath(device)
			try:
				entry['tv'] = samsungtvws.SamsungTVWS(
					host=ipaddress, port=self._getPort(device),
					token_file=token_file, timeout=self.timeout
				)
			except Exception:
				self.logger.debug(f"Failed to create TV connection for {device.name}")
				entry['tv'] = None
		return entry.get('tv')

	def _closeTV(self, device):
		entry = self.deviceList.get(device.id)
		if not entry:
			return
		tv = entry.get('tv')
		if tv is not None:
			try:
				tv.close()
			except Exception:
				pass
			entry['tv'] = None

	# ---------------------------------------------------------------------------
	# State polling
	# ---------------------------------------------------------------------------
	def updatedevicestates(self, device):
		# Serialize polls per device: this can be entered concurrently from
		# runConcurrentThread, wakeUp(), toggleDetailedDebugging(), the
		# refresh_tv_data action, and the background poll kicked off by
		# deviceStartComm() — without a lock, two overlapping polls would race
		# on the same TV connection and on replacePluginPropsOnServer() below.
		entry = self.deviceList.setdefault(device.id, {})
		lock = entry.setdefault('lock', threading.Lock())
		with lock:
			self._pollDeviceStates(device)

	def _pollDeviceStates(self, device):
		ipaddress  = device.pluginProps['ipaddress']
		token_file = self._getTokenPath(device)

		self.logger.debug(f"Polling {device.name} ({device.id})")

		try:
			tv   = samsungtvws.SamsungTVWS(host=ipaddress, port=self._getPort(device), token_file=token_file, timeout=self.timeout)
			info = tv.rest_device_info()
			tvState = info['device']['PowerState'].lower()
			self.logger.debug(f"Power state of {device.name}: {tvState}")
		except Exception as e:
			entry = self.deviceList.setdefault(device.id, {})
			retry_count = entry.get('retryCount', 0)
			detail, root = self._describeException(e)
			is_tls_error = "SSL" in type(root).__name__ or "ssl" in str(root).lower()
			if retry_count == 0:
				# First failure since the last successful poll (or ever, on a
				# fresh device) — log it loudly. This is the only signal a user
				# gets that the configured IP address is wrong, so it must not
				# be debug-only.
				if is_tls_error:
					self.logger.error(f"\"{device.name}\": TLS/SSL handshake with {ipaddress} failed ({detail}) — this usually means a Python library update changed TLS behavior, not that the IP address or TV is wrong")
				else:
					self.logger.error(f"\"{device.name}\": cannot reach {ipaddress} — check that the IP address is correct, the TV is powered on, and it's reachable on the network ({detail})")
			else:
				self.logger.debug(f"\"{device.name}\": still unreachable at {ipaddress} (attempt {retry_count + 1}): {detail}")
			device.setErrorStateOnServer("Unreachable")
			device.updateStatesOnServer([
				{'key': 'status',     'value': 'off'},
				{'key': 'onOffState', 'value': False},
			])
			device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
			entry['retryCount'] = retry_count + 1
			return

		# Clear any previous error badge, and announce success the same way
		# failures are announced — a successful poll was previously silent
		# (debug-only), so there was no positive confirmation in the log that
		# a TV actually connected on a given reload, only an absence of errors.
		device.setErrorStateOnServer(None)
		entry = self.deviceList.setdefault(device.id, {})
		if entry.get('retryCount', 0) > 0:
			self.logger.info(f"\"{device.name}\": reachable again at {ipaddress}")
		elif not entry.get('everConnected'):
			# First successful poll since this device started (e.g. right
			# after a plugin reload) — confirm it loudly once, then go quiet
			# for routine repeat successes so the log isn't spammed every
			# refresh_frequency seconds for a TV that's working fine.
			self.logger.info(f"\"{device.name}\": connected successfully at {ipaddress}")
		entry['everConnected'] = True

		# Persist MAC into pluginProps so WOL works even before first successful poll
		mac = info['device'].get('wifiMac', '')
		if mac and device.pluginProps.get('macaddress', '') != mac:
			props = device.pluginProps
			props['macaddress'] = mac
			device.replacePluginPropsOnServer(props)

		# Detect currently running app and read volume — only meaningful when the TV is on
		current_app = ""
		if tvState == 'on' and device.pluginProps.get('detectApp', True):
			current_app = self._getCurrentApp(ipaddress, device)

		volume = self._getVolumeUPnP(ipaddress) if tvState == 'on' else None
		if volume is not None:
			self.logger.debug(f"Volume for {device.name}: {volume}")

		muted = self._getMuteUPnP(ipaddress) if tvState == 'on' else None
		if muted is not None:
			self.logger.debug(f"Mute state for {device.name}: {muted}")

		# If an app is running the TV is on the Smart Hub, not a physical input.
		# Only overwrite currentSource when we can determine it; otherwise leave it
		# as whatever Indigo last set via selectSource so HDMI state persists.
		if current_app:
			current_source = "Smart Hub"
		else:
			current_source = device.states.get("currentSource", "Smart Hub") or "Smart Hub"

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
			{'key': 'currentSource',   'value': current_source},
		]
		if volume is not None:
			device_states.append({'key': 'volumeLevel', 'value': volume})
		if muted is not None:
			device_states.append({'key': 'isMuted', 'value': muted})

		if self.detailedDebug:
			self.logger.info(f"--- States for {device.name} ---")
			for state in device_states:
				self.logger.info(f"  {state['key']:<20} = {state['value']}")
			self.logger.info(f"--- End states for {device.name} ---")

		device.updateStatesOnServer(device_states)
		device.updateStateImageOnServer(
			indigo.kStateImageSel.SensorOn if tvState == 'on' else indigo.kStateImageSel.SensorOff
		)

		if device.id in self.deviceList:
			self.deviceList[device.id]['lastUpdate'] = str(datetime.datetime.now())
			self.deviceList[device.id]['retryCount'] = 0

	def _getCurrentApp(self, ip, device):
		"""Poll the TV for the foreground app from COMMON_APPS. Returns app name or empty string.

		Short-circuits on the first visible app instead of always querying every entry in
		COMMON_APPS — in the common case (same app still foreground since the last poll)
		this costs one REST call instead of up to len(COMMON_APPS). Each probe also uses its
		own short timeout, independent of self.timeout, so a slow/unresponsive TV can't turn
		this into len(COMMON_APPS) * self.timeout seconds on every single poll.

		Two-pass fallback: visible (foreground) takes priority over running (background/
		cached) so that an app like YouTube sitting in the background doesn't shadow the
		active app.
		"""
		entry = self.deviceList.setdefault(device.id, {})
		last_app = entry.get('lastApp')

		# Probe the last known app first so an unchanged foreground app resolves in one call.
		ordered_names = list(COMMON_APPS.keys())
		if last_app in COMMON_APPS:
			ordered_names.remove(last_app)
			ordered_names.insert(0, last_app)

		probe_timeout = min(self.timeout, 1.0)
		rest = samsungtvws.rest.SamsungTVRest(host=ip, port=self._getPort(device), timeout=probe_timeout)

		running_fallback = ""
		for app_name in ordered_names:
			try:
				status = rest.rest_app_status(COMMON_APPS[app_name])
			except Exception:
				continue
			if not status:
				continue
			if status.get('visible'):
				entry['lastApp'] = app_name
				return app_name
			if not running_fallback and status.get('running'):
				running_fallback = app_name

		entry['lastApp'] = running_fallback or None
		return running_fallback

	def _upnpAction(self, ip, action, extra_body=""):
		"""Call a UPnP RenderingControl:1 action on the TV (port 9197).
		Returns the parsed XML response root, or None on failure."""
		soap = (
			'<?xml version="1.0" encoding="utf-8"?>'
			'<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"'
			' xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
			'<s:Body>'
			f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
			'<InstanceID>0</InstanceID><Channel>Master</Channel>'
			f'{extra_body}'
			f'</u:{action}>'
			'</s:Body>'
			'</s:Envelope>'
		)
		try:
			req = urllib.request.Request(
				f"http://{ip}:{UPNP_PORT}/upnp/control/RenderingControl1",
				data=soap.encode('utf-8'),
				headers={
					'Content-Type': 'text/xml; charset="utf-8"',
					'SOAPAction': f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"',
				},
				method='POST'
			)
			with urllib.request.urlopen(req, timeout=2) as resp:
				return ET.fromstring(resp.read())
		except Exception:
			return None

	def _getVolumeUPnP(self, ip):
		"""Read current volume from the TV via UPnP RenderingControl.
		Returns an int 0-100, or None if the TV does not respond."""
		root = self._upnpAction(ip, "GetVolume")
		if root is None:
			return None
		el = root.find('.//CurrentVolume')
		return int(el.text) if el is not None else None

	def _setVolumeUPnP(self, ip, volume):
		"""Set an absolute volume (0-100) via UPnP RenderingControl.
		Returns True if the TV acknowledged the request."""
		root = self._upnpAction(ip, "SetVolume", f"<DesiredVolume>{int(volume)}</DesiredVolume>")
		return root is not None

	def _getMuteUPnP(self, ip):
		"""Read current mute state via UPnP RenderingControl.
		Returns True/False, or None if the TV does not respond."""
		root = self._upnpAction(ip, "GetMute")
		if root is None:
			return None
		el = root.find('.//CurrentMute')
		if el is None:
			return None
		return el.text in ("1", "true", "True")

	def _setMuteUPnP(self, ip, muted):
		"""Set mute state via UPnP RenderingControl.
		Returns True if the TV acknowledged the request."""
		root = self._upnpAction(ip, "SetMute", f"<DesiredMute>{1 if muted else 0}</DesiredMute>")
		return root is not None

	def refresh_tv_data(self, pluginAction, device):
		self.updatedevicestates(device)

	# ---------------------------------------------------------------------------
	# runDiagnostics action — step-by-step connectivity check, written to the
	# event log. Actions.xml entries are NOT a device right-click menu item in
	# Indigo — they're only reachable by adding a step to an Action Group,
	# Trigger, or Schedule (confirmed against the real IndigoSDK examples and
	# the Indigo docs; see the "Run Diagnostics on All TVs" Plugins-menu item
	# below for a true zero-setup way to run this).
	# ---------------------------------------------------------------------------
	def runDiagnostics(self, pluginAction, device):
		self._runDiagnosticsForDevice(device)

	def runDiagnosticsOnAllTVs(self):
		"""Plugins → SamsungTV → Run Diagnostics on All TVs — the actual
		one-click, no-setup path, since Actions.xml entries require an Action
		Group to be reachable at all."""
		devices = [d for d in indigo.devices.iter("self") if d.deviceTypeId == "televisions"]
		if not devices:
			self.logger.info("No Samsung TV devices are configured")
			return
		for dev in devices:
			self._runDiagnosticsForDevice(dev)

	def _runDiagnosticsForDevice(self, device):
		name = device.name
		ip = device.pluginProps.get('ipaddress', '').strip()
		configured_port = self._getPort(device)

		self.logger.info("")
		self.logger.info("{0:=^100}".format(f" Diagnostics: \"{name}\" "))

		# 1. IP address format
		try:
			ipaddress_module.ip_address(ip)
			self.logger.info(f"[1] IP address format........... OK ({ip})")
		except ValueError:
			self.logger.info(f"[1] IP address format........... FAILED — \"{ip}\" is not a valid IP address")
			self.logger.info("{0:=^100}".format(""))
			self.logger.error(f"\"{name}\": fix the IP address in this device's config before diagnostics can continue")
			return

		# 2. TCP reachability on the configured port — bypasses HTTP/TLS/the
		# Tizen API entirely, isolating "is anything there at all" from
		# "does the TV's API respond correctly".
		port_ok, port_detail = self._checkTcpPort(ip, configured_port, timeout=3.0)
		self.logger.info(f"[2] TCP port {configured_port} reachable......... {'OK' if port_ok else 'FAILED'} ({port_detail})")

		# 3. The other common port, as a hint if the Port setting is simply wrong
		other_port = 8001 if configured_port == 8002 else 8002
		other_ok, other_detail = self._checkTcpPort(ip, other_port, timeout=3.0)
		self.logger.info(f"[3] TCP port {other_port} reachable (alt)..... {'OK' if other_ok else 'FAILED'} ({other_detail})")

		# 4. UPnP RenderingControl port — a separate service/port from the
		# main REST/WebSocket port, so this can be broken independently.
		upnp_ok, upnp_detail = self._checkTcpPort(ip, UPNP_PORT, timeout=3.0)
		self.logger.info(f"[4] UPnP port {UPNP_PORT} reachable......... {'OK' if upnp_ok else 'FAILED'} ({upnp_detail})")

		# 5. Token file
		token_path = self._getTokenPath(device)
		token_ok = os.path.exists(token_path) and os.access(token_path, os.R_OK | os.W_OK)
		self.logger.info(f"[5] Token file accessible....... {'OK' if token_ok else 'FAILED'} ({token_path})")

		# 6. MAC address stored (required for Wake-on-LAN)
		mac = device.pluginProps.get('macaddress', '') or device.states.get('macaddress', '')
		self.logger.info(f"[6] MAC address stored.......... {'OK (' + mac + ')' if mac else 'MISSING — Wake-on-LAN will not work until a successful poll discovers it'}")

		# 7. The actual Tizen REST API call that polling depends on — only
		# worth trying if the port is even open.
		rest_ok = False
		rest_detail = "skipped — port unreachable"
		if port_ok:
			try:
				rest = samsungtvws.rest.SamsungTVRest(host=ip, port=configured_port, timeout=3.0)
				info = rest.rest_device_info()
				rest_ok = True
				rest_detail = f"PowerState={info.get('device', {}).get('PowerState', 'unknown')}"
			except Exception as e:
				rest_detail, _ = self._describeException(e)
		self.logger.info(f"[7] Tizen REST API (/api/v2/)... {'OK' if rest_ok else 'FAILED'} ({rest_detail})")

		self.logger.info("{0:=^100}".format(""))

		# Plain-language summary of the most likely cause, based on which
		# checks passed — so the log doesn't just dump data, it points
		# somewhere useful.
		if rest_ok:
			self.logger.info(f"\"{name}\": diagnostics passed — this TV is fully reachable right now.")
		elif port_ok:
			self.logger.error(f"\"{name}\": the TV accepts a connection on port {configured_port}, but the Tizen API call itself failed (see [7] above) — likely a TLS/library issue, not a wrong IP or offline TV.")
		elif other_ok:
			self.logger.error(f"\"{name}\": port {configured_port} is closed but port {other_port} answered — try changing the Port setting on this device to {other_port}.")
		else:
			self.logger.error(f"\"{name}\": not reachable on any checked port right now. This points to a network/device issue, not the plugin — check that the TV is powered on, connected to Wi-Fi/Ethernet, and that its IP ({ip}) matches what the TV itself reports in its network settings (it may have changed via DHCP).")

	# ---------------------------------------------------------------------------
	# Relay / switch built-in action handler
	# ---------------------------------------------------------------------------
	def actionControlDevice(self, action, dev):
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			self._reportPowerResult(dev, self.turnOnTV(dev), turning_on=True)
		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			self._reportPowerResult(dev, self.turnOffTV(dev), turning_on=False)

	# ---------------------------------------------------------------------------
	# Power actions
	# ---------------------------------------------------------------------------
	def toggleTVPower(self, pluginAction, device):
		if device.onState:
			self._reportPowerResult(device, self.turnOffTV(device), turning_on=False)
		else:
			self._reportPowerResult(device, self.turnOnTV(device), turning_on=True)

	def turnOnTVAction(self, pluginAction, device, callerWaitingForResult=None):
		self._reportPowerResult(device, self.turnOnTV(device), turning_on=True)

	def turnOffTVAction(self, pluginAction, device, callerWaitingForResult=None):
		self._reportPowerResult(device, self.turnOffTV(device), turning_on=False)

	def _reportPowerResult(self, device, result, turning_on):
		"""Log/update state for the outcome of turnOnTV()/turnOffTV().

		result is one of "sent" (command dispatched), "already" (device was
		already in the desired state — a no-op, not a failure), or "failed".
		"""
		verb = "on" if turning_on else "off"
		if result == "sent":
			self.logger.info(f"sent \"{device.name}\" {verb}")
			device.updateStateOnServer("onOffState", turning_on)
			device.updateStateOnServer("status", "on" if turning_on else "off")
		elif result == "failed":
			self.logger.error(f"send \"{device.name}\" {verb} failed")
			if turning_on:
				self.logger.info(f"Verify the IP address for \"{device.name}\" ({device.pluginProps.get('ipaddress', 'unknown')}) has not changed and remote permissions have been allowed")
		# "already": turnOnTV()/turnOffTV() already logged an info line — nothing more to do.

	def volumeUpAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeUp(device)

	def volumeDownAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeDown(device)

	def turnOnTV(self, device):
		"""Returns "sent", "already" (already on — not a failure), or "failed"."""
		if device.states.get('status') == "on":
			self.logger.info(f"\"{device.name}\" is already on")
			return "already"
		macaddress = device.pluginProps.get('macaddress', '') or device.states.get('macaddress', '')
		if not macaddress:
			self.logger.error(f"\"{device.name}\": no MAC address stored — turn the TV on manually, wait for a poll cycle, then try again")
			return "failed"
		self.logger.info(f"Sending WOL magic packet to {macaddress} for \"{device.name}\"")
		try:
			wakeonlan.send_magic_packet(macaddress)
			return "sent"
		except Exception as m:
			self.logger.error(f"WOL for \"{device.name}\" failed: {m}")
			return "failed"

	def turnOffTV(self, device):
		"""Returns "sent", "already" (already off — not a failure), or "failed"."""
		if device.states.get('status') != "on":
			self.logger.info(f"\"{device.name}\" is not on")
			return "already"
		self.logger.info(f"Sending power key to \"{device.name}\"")
		tv = self._getTV(device)
		if not tv:
			self.logger.error(f"Power off for \"{device.name}\" failed: cannot connect")
			return "failed"
		try:
			tv.shortcuts().power()
			return "sent"
		except Exception as m:
			self.logger.error(f"Power off for \"{device.name}\" failed: {m}")
			return "failed"

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
			# The cached connection may have gone stale (TV rebooted, standby, etc).
			# Reconnect once and retry before giving up.
			self.logger.debug(f"{label} failed on \"{device.name}\" ({m}) — reconnecting and retrying")
			tv = self._getTV(device, forceNew=True)
			if not tv:
				self.logger.error(f"Cannot reach \"{device.name}\" to send {label}")
				return False
			try:
				tv.send_key(key)
				return True
			except Exception as m2:
				self.logger.error(f"{label} failed on \"{device.name}\": {m2}")
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

		ipaddress = device.pluginProps.get('ipaddress', '')

		# Preferred path: UPnP SetVolume is a single absolute request — no risk of
		# dropped key presses or racing the device's actual volume.
		if ipaddress and self._setVolumeUPnP(ipaddress, target):
			self.logger.debug(f"Volume set to {target} via UPnP on \"{device.name}\"")
			device.updateStateOnServer("volumeLevel", target)
			return

		# Fallback: step via remote keys when UPnP control isn't available on this TV.
		current = device.states.get("volumeLevel", 50)
		if not isinstance(current, int):
			current = 50

		diff = target - current
		if diff == 0:
			return

		tv = self._getTV(device)
		if not tv:
			return

		self.logger.debug(f"UPnP volume control unavailable — stepping {current} → {target} ({diff:+d}) via remote keys on \"{device.name}\"")
		try:
			key  = "KEY_VOLUP" if diff > 0 else "KEY_VOLDOWN"
			# samsungtvws defaults to a 1-second pause after every send_key() —
			# fine for a single button press, but this loop can run up to 100
			# times (a full 0-100 jump), so left at the default it would block
			# this action for up to ~100 seconds. 100ms is still comfortably
			# enough for the TV to register each discrete step.
			for _ in range(abs(diff)):
				tv.send_key(key, key_press_delay=0.1)
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
			device.updateStateOnServer("currentApp", "")

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
			ipaddress = device.pluginProps.get('ipaddress', '')
			muted = self._getMuteUPnP(ipaddress) if ipaddress else None
			if muted is None:
				# UPnP didn't answer — fall back to the old blind-toggle guess;
				# the next poll will correct it from the TV's real state anyway.
				muted = not device.states.get("isMuted", False)
			device.updateStateOnServer("isMuted", muted)

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
