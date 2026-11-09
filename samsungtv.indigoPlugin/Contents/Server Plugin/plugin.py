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
import json
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import socket
import ipaddress as ipaddress_module
import threading
import time

import requests
import samsungtvws
import wakeonlan
from samsungtvws.exceptions import HttpApiError, UnauthorizedError

from transports import (
	AsyncRunner, WebsocketTransport, EncryptedTransport, EncryptedPairing, LegacyTransport,
	NeedsPairingError, NotSupportedError, model_requires_encryption,
	METHOD_AUTO, METHOD_WEBSOCKET, METHOD_ENCRYPTED, METHOD_LEGACY,
	WEBSOCKET_SSL_PORT, WEBSOCKET_NO_SSL_PORT, ENCRYPTED_PORT, ENCRYPTED_PAIRING_PORT, LEGACY_PORT,
)

# Maps display names shown in the selectSource action to Samsung key codes.
SOURCE_MAP = {
	"TV":          "KEY_TV",
	"HDMI":        "KEY_HDMI",      # cycles through HDMI inputs (what HA exposes)
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

CUSTOM_APP = "__custom__"

# "Turn On using" menu value meaning "no action group — use Wake-on-LAN".
TURN_ON_WOL = "wol"

# Upper bound for network work done inside a config-dialog button callback.
# The Indigo client stops waiting for a callback after a few seconds; past
# that, it logs a timeout and mis-matches the late reply to its next request.
DIALOG_TIMEOUT = 6

# How long a background "press Allow on the TV" wait stays open.
AUTHORIZE_WAIT = 30

# UPnP RenderingControl port used by Samsung TVs (2014+).
UPNP_PORT = 9197

# After a power-off is sent, TVs keep answering the network for several
# seconds while they shut down. Treat the TV as off for this long (and drop
# key presses) so a poll doesn't flip it straight back to "on" — same idea as
# HA's power_off_in_progress window.
POWER_OFF_GRACE = 25

METHOD_LABELS = {
	METHOD_WEBSOCKET: "Websocket (2016+ Tizen)",
	METHOD_ENCRYPTED: "Encrypted (2014-2015 H/J series)",
	METHOD_LEGACY:    "Legacy (2013 and earlier)",
}


class UnexpectedResponse(Exception):
	"""The TV answered, but not with something we understand."""


class Plugin(indigo.PluginBase):

	# ---------------------------------------------------------------------------
	# Init — instance variables only; no Indigo DB access, no network calls
	# ---------------------------------------------------------------------------
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		level = pluginPrefs.get("debugLevel", "off")
		self.debug         = level in ("debug", "detailed")
		self.detailedDebug = level == "detailed"
		# dict keyed by device.id → per-device runtime state (locks, cached
		# transport, retry counters, power-off grace window, ...)
		self.deviceList = {}
		# In-progress encrypted PIN pairings from the device config dialog, keyed by IP.
		self._pairings = {}
		# IPs with a background "press Allow on the TV" wait in progress.
		self._authorizing = set()
		self._authorizingLock = threading.Lock()
		self.runner = AsyncRunner()
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
		self.logger.info("{0:<30} {1}".format("samsungtvws version:", self._libVersion()))
		self.logger.info("{0:<30} {1}".format("Indigo version:",    indigo.server.version))
		self.logger.info("{0:<30} {1}".format("Python version:",    sys.version.replace('\n', '')))
		self.logger.info("{0:<30} {1}".format("Python directory:",  sys.prefix.replace('\n', '')))
		self.logger.info("{0:=^130}".format(""))

		if not self.runner.available:
			from transports import ASYNC_IMPORT_ERROR
			self.logger.warning(
				f"2014-2015 (H/J) and pre-2014 TV support is unavailable — samsungtvws async/encrypted "
				f"extras failed to import: {ASYNC_IMPORT_ERROR}"
			)

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
		self.runner.stop()

	def prepareToSleep(self):
		self.logger.debug("Preparing for sleep — closing cached TV connections")
		for devId in list(self.deviceList.keys()):
			if devId in indigo.devices:
				self._closeTV(indigo.devices[devId])

	def wakeUp(self):
		self.logger.debug("Waking from sleep — refreshing all TV states")
		for devId in list(self.deviceList.keys()):
			if devId in indigo.devices:
				self._backgroundPoll(indigo.devices[devId])

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
			self._closeTV(device)
			entry = self.deviceList.setdefault(device.id, {})
			entry["retryCount"] = 0
			entry["lastUpdate"] = None
			entry.pop("appListRequested", None)
			entry.pop("macChecked", None)
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
		threading.Thread(target=self._safePoll, args=(device.id, delay), daemon=True).start()

	def _schedulePolls(self, device, delays):
		"""Poll again shortly after a power change so the state catches up in
		seconds instead of waiting up to a full refresh interval."""
		for d in delays:
			self._backgroundPoll(device, delay=d)

	def _safePoll(self, devId, delay=0):
		try:
			if delay:
				time.sleep(delay)
			if devId in indigo.devices and devId in self.deviceList:
				self.updatedevicestates(indigo.devices[devId])
		except Exception:
			self.logger.exception(f"Error during background poll of device {devId}")

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

			port_str = valuesDict.get('port', '').strip() or str(WEBSOCKET_SSL_PORT)
			try:
				if not (1 <= int(port_str) <= 65535):
					raise ValueError
			except ValueError:
				self.logger.error(f"Samsung TV device config: \"{port_str}\" is not a valid port (must be 1-65535)")
				errorsDict = indigo.Dict()
				errorsDict['port'] = "Port must be a number between 1 and 65535"
				return (False, valuesDict, errorsDict)
			valuesDict['port'] = port_str

			wol = valuesDict.get('wolBroadcast', '').strip()
			if wol:
				try:
					ipaddress_module.ip_address(wol)
				except ValueError:
					errorsDict = indigo.Dict()
					errorsDict['wolBroadcast'] = "Enter a broadcast IP address (e.g. 192.168.1.255) or leave blank"
					return (False, valuesDict, errorsDict)

			# If the IP or the forced protocol changed, the previously detected
			# protocol/port no longer applies — clear it so the next poll re-detects.
			if devId and devId in indigo.devices:
				old = indigo.devices[devId].pluginProps
				if old.get('ipaddress', '') != ip_str or old.get('method', METHOD_AUTO) != valuesDict.get('method', METHOD_AUTO):
					if not valuesDict.get('detectedFor', '') == ip_str:
						valuesDict['resolvedMethod'] = ""
						valuesDict['restPort'] = ""

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

			method = valuesDict.get('method', METHOD_AUTO)
			effective = method if method != METHOD_AUTO else valuesDict.get('resolvedMethod', '')
			if effective == METHOD_ENCRYPTED and not (valuesDict.get('encToken') and valuesDict.get('encSessionId')):
				self.logger.warning(
					f"Samsung TV at {ip_str} uses the 2014-2015 encrypted protocol and isn't PIN-paired yet — "
					"status will work, but remote keys won't until you pair (Edit Device → Pair / Authorize)"
				)

		return (True, valuesDict)

	def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
		"""Initial values when the device dialog opens. Indigo doesn't reliably
		apply a Devices.xml defaultValue to a menu whose options come from a
		dynamic list, so "Turn On using" opened blank on new devices. Set it
		here whenever the stored value isn't a real option — new devices,
		devices created before the option existed, or an action group that has
		since been deleted."""
		valuesDict = indigo.Dict(pluginProps)
		errorsDict = indigo.Dict()
		if typeId == "televisions":
			current = valuesDict.get('turnOnActionGroup', '')
			valid = {value for value, _label in self.getActionGroupList()}
			if current not in valid:
				valuesDict['turnOnActionGroup'] = TURN_ON_WOL
		return (valuesDict, errorsDict)

	def closedDeviceConfigUi(self, valuesDict, userCancelled, typeId, devId):
		self._pairings.pop(valuesDict.get('ipaddress', '').strip(), None)

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
	# Device config dialog buttons: Detect TV / Pair / Submit PIN
	# ---------------------------------------------------------------------------
	def detectTVButton(self, valuesDict, typeId, devId):
		ip = valuesDict.get('ipaddress', '').strip()
		if not ip:
			valuesDict['detectedInfo'] = "Enter the TV's IP address first."
			return valuesDict
		try:
			port = int(valuesDict.get('port', '') or WEBSOCKET_SSL_PORT)
		except ValueError:
			port = WEBSOCKET_SSL_PORT
		try:
			# Short per-probe timeout: this runs inside a dialog button callback,
			# which must answer the Indigo client within a few seconds even when
			# every probe fails (TV off).
			result = self._detect(ip, valuesDict.get('method', METHOD_AUTO), port, timeout=1.5)
		except Exception as e:
			detail, _ = self._describeException(e)
			valuesDict['detectedInfo'] = f"No response from {ip}. Make sure the TV is ON and the IP is right. ({detail[:160]})"
			return valuesDict

		dev = (result['info'] or {}).get('device', {})
		model = dev.get('modelName', '') or "unknown model"
		valuesDict['resolvedMethod'] = result['method']
		valuesDict['restPort'] = str(result['restPort'] or "")
		valuesDict['model'] = dev.get('modelName', '')
		valuesDict['frameTV'] = "true" if dev.get('FrameTVSupport') == "true" else "false"
		valuesDict['detectedFor'] = ip
		mac = dev.get('wifiMac', '') or self._macFromArp(ip)
		if mac and not valuesDict.get('macaddress'):
			valuesDict['macaddress'] = mac

		msg = f"{dev.get('name', 'Samsung TV')} — {model} — {METHOD_LABELS[result['method']]}"
		if result['method'] == METHOD_ENCRYPTED:
			paired = valuesDict.get('encToken') and valuesDict.get('encSessionId')
			msg += ". Already paired." if paired else ". Needs PIN pairing: click Pair / Authorize."
		elif result['method'] == METHOD_LEGACY:
			msg += ". Click Pair / Authorize and press Allow on the TV."
		elif result['restPort'] == WEBSOCKET_SSL_PORT:
			msg += ". Click Pair / Authorize and press Allow on the TV if prompted."
		valuesDict['detectedInfo'] = msg
		return valuesDict

	def pairTVButton(self, valuesDict, typeId, devId):
		ip = valuesDict.get('ipaddress', '').strip()
		if not ip:
			valuesDict['pairingStatus'] = "Enter the TV's IP address first."
			return valuesDict
		method = valuesDict.get('method', METHOD_AUTO)
		if method == METHOD_AUTO:
			# Don't chain a detection in here — detect + pair in one callback
			# could exceed the client's response window. Detect is its own click.
			# The stored protocol is trustworthy if Detect TV was run for this IP
			# in the dialog, or polling detected it for the device's saved IP.
			saved_ip = indigo.devices[devId].pluginProps.get('ipaddress', '') if devId in indigo.devices else ''
			known_for = valuesDict.get('detectedFor') or saved_ip
			if not valuesDict.get('resolvedMethod') or known_for != ip:
				valuesDict['pairingStatus'] = "Click Detect TV first (with the TV turned on), then Pair / Authorize."
				return valuesDict
			method = valuesDict['resolvedMethod']

		# Every button callback must answer the Indigo client within a few
		# seconds — if it doesn't, the client times out ("timeout waiting for
		# plugin response ... UiAction") and then mis-pairs the late reply with
		# its next request ("unexpected packet response UiAction (waiting for
		# GetUiDynamicList)"). So the open-ended wait for someone to press
		# Allow on the TV runs in the background and reports to the event log.
		# Nothing needs to come back to the dialog: websocket TVs write the
		# granted token straight to the token file, and legacy TVs remember the
		# allowed remote themselves.
		try:
			if method == METHOD_ENCRYPTED:
				pairing = EncryptedPairing(self.runner, ip)
				pairing.start(timeout=DIALOG_TIMEOUT)
				self._pairings[ip] = pairing
				valuesDict['pairingStatus'] = "A PIN should now be showing on the TV. Type it into the PIN field and click Submit PIN."
			elif method == METHOD_LEGACY:
				started = self._authorizeInBackground(
					ip, lambda: LegacyTransport(self.runner, ip).authorize(timeout=AUTHORIZE_WAIT))
				valuesDict['pairingStatus'] = self._authorizeStatus(started)
			elif method == METHOD_WEBSOCKET:
				port = int(valuesDict.get('restPort') or valuesDict.get('port') or WEBSOCKET_SSL_PORT)
				token_name = os.path.basename(valuesDict.get('token', '').strip()) or f"tv-token-{ip.replace('.', '-')}.txt"
				token_path = self.path + token_name
				if not os.path.exists(token_path):
					open(token_path, 'w').close()
				valuesDict['token'] = token_name

				def authorize_ws():
					# The long timeout is the window for pressing Allow; the
					# library writes the granted token to the token file.
					tv = samsungtvws.SamsungTVWS(host=ip, port=port, token_file=token_path, timeout=AUTHORIZE_WAIT)
					try:
						tv.open()
					finally:
						tv.close()
				started = self._authorizeInBackground(ip, authorize_ws)
				valuesDict['pairingStatus'] = self._authorizeStatus(started)
			else:
				valuesDict['pairingStatus'] = "Couldn't determine the TV's protocol — click Detect TV with the TV turned on."
		except Exception as e:
			detail, _ = self._describeException(e)
			valuesDict['pairingStatus'] = f"Pairing failed: {detail[:200]}"
		return valuesDict

	def _authorizeStatus(self, started):
		if not started:
			return "Already waiting for this TV — press Allow on the TV. The result will appear in the Indigo event log."
		return (f"Press Allow on the TV now (you have {AUTHORIZE_WAIT} seconds). The result will appear in the "
				"Indigo event log — you can click Save at any time.")

	def _authorizeInBackground(self, ip, authorize):
		"""Run a blocking "wait for Allow on the TV" outside the dialog
		callback. Returns False if one is already running for this IP."""
		with self._authorizingLock:
			if ip in self._authorizing:
				return False
			self._authorizing.add(ip)

		def run():
			try:
				authorize()
				self.logger.info(f"Samsung TV at {ip}: remote control authorized")
			except UnauthorizedError:
				self.logger.error(
					f"Samsung TV at {ip} refused the remote. On the TV: Settings → General → External Device Manager → "
					"Device Connection Manager — allow (or remove) the entry, then click Pair / Authorize again."
				)
			except Exception as e:
				detail, _ = self._describeException(e)
				if self._isNetworkError(e):
					self.logger.error(f"Samsung TV at {ip}: no Allow received within {AUTHORIZE_WAIT} s, or the TV stopped responding ({detail}). Click Pair / Authorize to try again.")
				else:
					self.logger.error(f"Samsung TV at {ip}: authorization failed: {detail}")
			finally:
				with self._authorizingLock:
					self._authorizing.discard(ip)

		threading.Thread(target=run, name=f"SamsungTV-authorize-{ip}", daemon=True).start()
		return True

	def submitPinButton(self, valuesDict, typeId, devId):
		ip = valuesDict.get('ipaddress', '').strip()
		pin = re.sub(r"\D", "", valuesDict.get('pin', ''))
		if not pin:
			valuesDict['pairingStatus'] = "Enter the PIN shown on the TV."
			return valuesDict
		pairing = self._pairings.get(ip)
		if pairing is None:
			valuesDict['pairingStatus'] = "No pairing in progress — click Pair / Authorize to show a PIN on the TV first."
			return valuesDict
		try:
			token, session_id = pairing.finish(pin, timeout=DIALOG_TIMEOUT)
		except Exception as e:
			detail, _ = self._describeException(e)
			self._pairings.pop(ip, None)
			valuesDict['pairingStatus'] = f"Pairing failed ({detail[:160]}). Click Pair / Authorize to get a new PIN."
			return valuesDict
		if not token:
			valuesDict['pairingStatus'] = "Wrong PIN — check the TV and try again."
			return valuesDict
		self._pairings.pop(ip, None)
		valuesDict['encToken'] = token
		valuesDict['encSessionId'] = str(session_id)
		valuesDict['pin'] = ""
		valuesDict['pairingStatus'] = "Paired! Click Save."
		return valuesDict

	def getActionGroupList(self, filter="", valuesDict=None, typeId="", targetId=0):
		groups = [(str(ag.id), ag.name) for ag in indigo.actionGroups]
		# A real sentinel value, not "": Indigo drops menu options whose value is
		# an empty string, which left no way to choose "none".
		return [(TURN_ON_WOL, "Wake-on-LAN (default)")] + sorted(groups, key=lambda g: g[1].lower())

	def _turnOnActionGroup(self, device):
		"""The action group chosen to turn this TV on, or None for Wake-on-LAN.
		Treats the sentinel, a blank/missing value (devices created before this
		option existed) and a since-deleted action group all as Wake-on-LAN."""
		value = device.pluginProps.get('turnOnActionGroup', '') or TURN_ON_WOL
		if value == TURN_ON_WOL:
			return None
		try:
			return indigo.actionGroups[int(value)]
		except (ValueError, KeyError, TypeError):
			self.logger.warning(f"\"{device.name}\": the turn-on action group (id {value}) no longer exists — using Wake-on-LAN")
			return None

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
			props = dev.pluginProps
			method = self._effectiveMethod(dev)
			self.logger.info("{0:<20} {1}".format("Device name:",  dev.name))
			self.logger.info("{0:<20} {1}".format("IP address:",   props.get('ipaddress', 'not set')))
			self.logger.info("{0:<20} {1}".format("Model:",        props.get('model', '') or dev.states.get('model', '') or 'unknown'))
			self.logger.info("{0:<20} {1}".format("Protocol:",     METHOD_LABELS.get(method, "not detected yet") + (" (forced)" if props.get('method', METHOD_AUTO) != METHOD_AUTO else "")))
			if method == METHOD_ENCRYPTED:
				self.logger.info("{0:<20} {1}".format("PIN paired:", "yes" if props.get('encToken') else "NO — Edit Device → Pair / Authorize"))
			self.logger.info("{0:<20} {1}".format("MAC address:",  props.get('macaddress', '') or dev.states.get('macaddress', 'not yet discovered')))
			self.logger.info("{0:<20} {1}".format("OS:",           dev.states.get('os', 'unknown')))
			apps = self._installedApps(dev)
			if apps:
				self.logger.info("{0:<20} {1}".format("Installed apps:", ", ".join(sorted(apps.keys()))))
			self.logger.info("{0:-^130}".format(""))
		self.logger.info("{0:=^130}".format(""))

	def discoverSamsungTVs(self):
		SSDP_ADDR = "239.255.255.250"
		SSDP_PORT = 1900
		SSDP_MX   = 4

		# Multiple STs maximize coverage across Samsung firmware versions.
		# ssdp:all catches everything; the narrow STs catch older models that only respond to them.
		search_targets = [
			"ssdp:all",
			"upnp:rootdevice",
			"urn:samsung.com:device:RemoteControlReceiver:1",
			"urn:dial-multiscreen-org:service:dial:1",
		]

		self.logger.info("Scanning network for Samsung TVs — this takes about 20 seconds...")

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
			sock = None
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
				if sock is not None:
					try:
						sock.close()
					except Exception:
						pass

		# Every configured TV is a candidate too, so a TV that ignores SSDP still
		# gets its protocol reported here.
		configured = {
			d.pluginProps.get('ipaddress', ''): d.name
			for d in indigo.devices.iter("self")
			if d.deviceTypeId == "televisions"
		}

		# Classify each candidate the same way device auto-detection does.
		found = {}
		for ip in sorted(candidate_ips | {ip for ip in configured if ip}):
			try:
				result = self._detect(ip, METHOD_AUTO, WEBSOCKET_SSL_PORT, timeout=2.0)
				found[ip] = result
			except Exception:
				self.logger.debug(f"{ip} did not answer as a Samsung TV — skipping")

		if not found:
			self.logger.info("No Samsung TVs found. Make sure TVs are powered on and on the same network segment.")
			return

		self.logger.info("{0:=^130}".format(f" Found {len(found)} Samsung TV(s) "))
		for ip, result in found.items():
			info = (result['info'] or {}).get('device', {})
			status = f"(configured as \"{configured[ip]}\")" if ip in configured else "(not yet configured)"
			self.logger.info(f"  IP Address:  {ip}  {status}")
			self.logger.info(f"  Name:        {info.get('name', 'unknown')}")
			self.logger.info(f"  Model:       {info.get('modelName', 'unknown')}")
			self.logger.info(f"  Protocol:    {METHOD_LABELS[result['method']]}")
			if result['method'] == METHOD_ENCRYPTED:
				self.logger.info("               needs one-time PIN pairing (Edit Device → Pair / Authorize)")
			self.logger.info(f"  MAC:         {info.get('wifiMac', '') or self._macFromArp(ip) or 'unknown'}")
			self.logger.info("{0:-^130}".format(""))
		self.logger.info("{0:=^130}".format(""))

	# ---------------------------------------------------------------------------
	# Small helpers
	# ---------------------------------------------------------------------------
	def _libVersion(self):
		try:
			from importlib.metadata import version
			return version("samsungtvws")
		except Exception:
			return "unknown"

	def _getPort(self, device):
		"""Returns the configured websocket port for a device, falling back to
		8002 (the default for Tizen TVs with token-based TLS auth) if unset or
		invalid."""
		try:
			port = int(device.pluginProps.get('port', WEBSOCKET_SSL_PORT) or WEBSOCKET_SSL_PORT)
			return port if 1 <= port <= 65535 else WEBSOCKET_SSL_PORT
		except (TypeError, ValueError):
			return WEBSOCKET_SSL_PORT

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

	def _isNetworkError(self, e):
		"""True when the failure means "nothing answered" (TV off / asleep /
		unplugged / wrong IP), as opposed to "the TV answered with something
		unexpected" — the two need very different messages."""
		seen = set()
		while e is not None and id(e) not in seen:
			seen.add(id(e))
			if isinstance(e, (OSError, TimeoutError, socket.timeout, HttpApiError,
							  requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
				return True
			e = e.__cause__ or e.__context__
		return False

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

	def _macFromArp(self, ip):
		"""The MAC actually answering at this IP, from the Mac's ARP cache.
		Better than the REST API's wifiMac for wired TVs: Samsung reports the
		Wi-Fi MAC even when the TV is on Ethernet, and Wake-on-LAN has to target
		the interface the TV is really using."""
		try:
			out = subprocess.run(["/usr/sbin/arp", "-n", ip], capture_output=True, text=True, timeout=3).stdout
		except Exception:
			return ""
		m = re.search(r"\sat\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\s", out)
		if not m:
			return ""
		return ":".join(octet.zfill(2) for octet in m.group(1).split(":")).upper()

	def _updateProps(self, device, changes):
		"""Write only the pluginProps that actually changed (each write is a
		server round-trip)."""
		props = device.pluginProps
		dirty = False
		for key, value in changes.items():
			if value is None:
				continue
			if props.get(key, "") != value:
				props[key] = value
				dirty = True
		if dirty:
			device.replacePluginPropsOnServer(props)
		return dirty

	# ---------------------------------------------------------------------------
	# Protocol detection — mirrors HA's async_get_device_info(): REST device
	# info on 8002 then 8001; H/J models switch to the encrypted protocol on
	# 8000; nothing on REST but 55000 open means a pre-2014 legacy TV.
	# ---------------------------------------------------------------------------
	def _probeRest(self, ip, ports, timeout):
		last_err = None
		for port in ports:
			try:
				info = samsungtvws.rest.SamsungTVRest(host=ip, port=port, timeout=timeout).rest_device_info()
				if not isinstance(info, dict) or not isinstance(info.get('device'), dict):
					raise UnexpectedResponse(f"/api/v2/ on port {port} returned no 'device' block: {str(info)[:200]}")
				return port, info
			except Exception as e:
				last_err = e
		raise last_err

	def _detect(self, ip, method_pref, configured_port, timeout=3.0):
		"""Returns {'method', 'restPort', 'info'}; raises if nothing answers."""
		if method_pref == METHOD_LEGACY:
			ok, detail = self._checkTcpPort(ip, LEGACY_PORT, timeout)
			if not ok:
				raise OSError(f"legacy port {LEGACY_PORT}: {detail}")
			return {'method': METHOD_LEGACY, 'restPort': None, 'info': None}

		ports = [configured_port] + [p for p in (WEBSOCKET_SSL_PORT, WEBSOCKET_NO_SSL_PORT) if p != configured_port]
		try:
			port, info = self._probeRest(ip, ports, timeout)
		except Exception as rest_err:
			if method_pref == METHOD_AUTO and self._checkTcpPort(ip, LEGACY_PORT, timeout)[0]:
				return {'method': METHOD_LEGACY, 'restPort': None, 'info': None}
			raise rest_err

		if method_pref in (METHOD_WEBSOCKET, METHOD_ENCRYPTED):
			method = method_pref
		elif model_requires_encryption(info['device'].get('modelName')) and self._checkTcpPort(ip, ENCRYPTED_PORT, timeout)[0]:
			method = METHOD_ENCRYPTED
		else:
			method = METHOD_WEBSOCKET
		return {'method': method, 'restPort': port, 'info': info}

	def _effectiveMethod(self, device):
		pref = device.pluginProps.get('method', METHOD_AUTO) or METHOD_AUTO
		if pref != METHOD_AUTO:
			return pref
		return device.pluginProps.get('resolvedMethod', '') or None

	# ---------------------------------------------------------------------------
	# Transport (remote-control connection) management
	# ---------------------------------------------------------------------------
	def _getTransport(self, device):
		device = indigo.devices[device.id]
		props = device.pluginProps
		entry = self.deviceList.setdefault(device.id, {})
		ip = props.get('ipaddress', '').strip()
		method = self._effectiveMethod(device)
		if method is None or (method != METHOD_LEGACY and not props.get('restPort')):
			# Never detected (TV was off since the device was created) — try now.
			result = self._detect(ip, props.get('method', METHOD_AUTO), self._getPort(device))
			self._applyDetection(device, result)
			device = indigo.devices[device.id]
			props = device.pluginProps
			method = result['method']

		if method == METHOD_WEBSOCKET:
			port = int(props.get('restPort') or self._getPort(device))
			key = (method, ip, port, props.get('token', ''), props.get('frameTV', ''))
		elif method == METHOD_ENCRYPTED:
			key = (method, ip, props.get('encToken', ''), props.get('encSessionId', ''), props.get('model', ''))
		else:
			key = (method, ip)

		if entry.get('transport') is not None and entry.get('transportKey') == key:
			return entry['transport']

		self._closeTV(device)
		if method == METHOD_WEBSOCKET:
			transport = WebsocketTransport(ip, port, self._getTokenPath(device), timeout=self.timeout,
										   frame_tv=props.get('frameTV') == "true")
		elif method == METHOD_ENCRYPTED:
			transport = EncryptedTransport(self.runner, ip, props.get('encToken', ''), props.get('encSessionId', ''),
										   model=props.get('model', ''))
		else:
			transport = LegacyTransport(self.runner, ip)
		entry['transport'] = transport
		entry['transportKey'] = key
		return transport

	def _getTV(self, device, forceNew=False):
		"""Websocket transport for features that only exist on 2016+ TVs
		(apps, text input, browser). Raises NotSupportedError otherwise."""
		if forceNew:
			self._closeTV(device)
		transport = self._getTransport(device)
		if not isinstance(transport, WebsocketTransport):
			raise NotSupportedError(f"this needs a 2016+ Tizen TV — \"{device.name}\" uses the {METHOD_LABELS[transport.method]} protocol")
		return transport

	def _closeTV(self, device):
		entry = self.deviceList.get(device.id)
		if not entry:
			return
		transport = entry.pop('transport', None)
		entry.pop('transportKey', None)
		if transport is not None:
			try:
				transport.close()
			except Exception:
				pass

	def _powerOffInProgress(self, entry):
		return entry.get('powerOffUntil', 0) > time.monotonic()

	# ---------------------------------------------------------------------------
	# State polling
	# ---------------------------------------------------------------------------
	def updatedevicestates(self, device):
		# Serialize polls per device: this can be entered concurrently from
		# runConcurrentThread, wakeUp(), toggleDetailedDebugging(), the
		# refresh_tv_data action, and the background polls kicked off by
		# deviceStartComm()/power actions — without a lock, two overlapping
		# polls would race on replacePluginPropsOnServer() below.
		entry = self.deviceList.setdefault(device.id, {})
		lock = entry.setdefault('lock', threading.Lock())
		with lock:
			# Always work from a fresh copy: pluginProps may have been updated
			# (detected protocol, MAC, app list) since the caller fetched it.
			if device.id in indigo.devices:
				device = indigo.devices[device.id]
			self._pollDeviceStates(device)

	def _applyDetection(self, device, result):
		info = (result.get('info') or {}).get('device', {})
		changes = {
			'resolvedMethod': result['method'],
			'restPort': str(result['restPort'] or ""),
		}
		if info.get('modelName'):
			changes['model'] = info['modelName']
		if info:
			changes['frameTV'] = "true" if info.get('FrameTVSupport') == "true" else "false"
		if self._updateProps(device, changes):
			self.logger.info(f"\"{device.name}\": detected {info.get('modelName', 'TV')} — using the {METHOD_LABELS[result['method']]} protocol")
			if result['method'] == METHOD_ENCRYPTED and not device.pluginProps.get('encToken'):
				self.logger.warning(
					f"\"{device.name}\" is a 2014-2015 model that needs one-time PIN pairing before remote "
					"keys will work: Edit Device → Pair / Authorize → enter the PIN shown on the TV → Submit PIN → Save"
				)

	def _pollDeviceStates(self, device):
		props = device.pluginProps
		ipaddress = props.get('ipaddress', '').strip()
		if not ipaddress:
			return
		entry = self.deviceList.setdefault(device.id, {})

		if self._powerOffInProgress(entry):
			self.logger.debug(f"\"{device.name}\": power-off in progress — skipping poll")
			self._setOff(device)
			return

		self.logger.debug(f"Polling {device.name} ({device.id})")

		method = self._effectiveMethod(device)
		info = None
		try:
			if method == METHOD_LEGACY:
				ok, detail = self._checkTcpPort(ipaddress, LEGACY_PORT, self.timeout)
				if not ok:
					raise OSError(f"port {LEGACY_PORT}: {detail}")
			elif method and props.get('restPort'):
				_port, info = self._probeRest(ipaddress, [int(props['restPort'])], self.timeout)
			else:
				result = self._detect(ipaddress, props.get('method', METHOD_AUTO), self._getPort(device), self.timeout)
				self._applyDetection(device, result)
				device = indigo.devices[device.id]
				props = device.pluginProps
				method, info = result['method'], result['info']
		except Exception as e:
			self._handlePollFailure(device, entry, e, ipaddress)
			return

		# Power state. Only 2018+ firmware reports PowerState; when it's
		# missing (2014-2017 TVs), answering at all means the TV is on — these
		# sets drop off the network in standby. This is the fix for TVs that
		# were permanently "unreachable" because the plugin used to require it.
		dev_info = (info or {}).get('device', {})
		power_state = (dev_info.get('PowerState') or "").strip().lower()
		if power_state:
			tvState = "on" if power_state == "on" else power_state
		else:
			tvState = "on"
		is_on = tvState == "on"
		self.logger.debug(f"Power state of {device.name}: {tvState}" + ("" if power_state else " (inferred — TV doesn't report PowerState)"))

		# Clear any previous error badge, and announce success the same way
		# failures are announced.
		device.setErrorStateOnServer(None)
		previous = device.states.get('status', '')
		if entry.get('retryCount', 0) > 0 and entry.get('lastFailureKind') == 'unexpected':
			self.logger.info(f"\"{device.name}\": responding normally again at {ipaddress}")
		elif not entry.get('everConnected'):
			self.logger.info(f"\"{device.name}\": connected at {ipaddress} — {METHOD_LABELS.get(method, method)}, TV is {tvState}")
		elif previous != tvState:
			self.logger.info(f"\"{device.name}\" is {tvState}")
		entry['everConnected'] = True
		entry['retryCount'] = 0
		entry.pop('lastFailureKind', None)

		# MAC for Wake-on-LAN: the MAC actually answering at this IP (ARP) beats
		# the REST wifiMac, which is wrong for TVs on Ethernet. Checked once per
		# device start.
		mac = props.get('macaddress', '')
		if not entry.get('macChecked'):
			entry['macChecked'] = True
			arp_mac = self._macFromArp(ipaddress)
			best = arp_mac or dev_info.get('wifiMac', '')
			if best and best.upper() != mac.upper():
				self._updateProps(device, {'macaddress': best})
				self.logger.debug(f"\"{device.name}\": MAC for Wake-on-LAN set to {best}" + (" (from ARP)" if arp_mac else " (from TV)"))
				device = indigo.devices[device.id]
				mac = best

		# Detect currently running app — only meaningful when the TV is on, and
		# the per-app REST status endpoint only exists on 2016+ (websocket) TVs.
		current_app = ""
		if is_on and method == METHOD_WEBSOCKET and props.get('detectApp', True):
			current_app = self._getCurrentApp(ipaddress, device)

		# Fetch the TV's own installed-app list once per device start (2016+).
		if is_on and method == METHOD_WEBSOCKET and not entry.get('appListRequested'):
			entry['appListRequested'] = True
			threading.Thread(target=self._refreshAppList, args=(device.id, False), daemon=True).start()

		volume = self._getVolumeUPnP(ipaddress) if is_on else None
		if volume is not None:
			self.logger.debug(f"Volume for {device.name}: {volume}")

		muted = self._getMuteUPnP(ipaddress) if is_on else None
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
			{'key': 'status',           'value': tvState},
			{'key': 'onOffState',       'value': is_on},
			{'key': 'connectionMethod', 'value': method or ""},
			{'key': 'macaddress',       'value': mac},
			{'key': 'currentApp',       'value': current_app},
			{'key': 'currentSource',    'value': current_source},
		]
		if dev_info:
			device_states += [
				{'key': 'name',            'value': dev_info.get('name', '')},
				{'key': 'os',              'value': dev_info.get('OS', '')},
				{'key': 'device_id',       'value': dev_info.get('id', '')},
				{'key': 'model',           'value': dev_info.get('modelName', '')},
				{'key': 'tokenauthsupport','value': dev_info.get('TokenAuthSupport', '')},
				{'key': 'voicesupport',    'value': dev_info.get('VoiceSupport', '')},
				{'key': 'firmwareversion', 'value': dev_info.get('firmwareVersion', '')},
				{'key': 'networktype',     'value': dev_info.get('networkType', '')},
				{'key': 'type',            'value': info.get('type', '')},
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
			indigo.kStateImageSel.SensorOn if is_on else indigo.kStateImageSel.SensorOff
		)
		entry['lastUpdate'] = str(datetime.datetime.now())

	def _setOff(self, device):
		device.updateStatesOnServer([
			{'key': 'status',     'value': 'off'},
			{'key': 'onOffState', 'value': False},
			{'key': 'currentApp', 'value': ''},
		])
		device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

	def _handlePollFailure(self, device, entry, e, ipaddress):
		detail, _root = self._describeException(e)
		retry_count = entry.get('retryCount', 0)
		if self._isNetworkError(e):
			# Nothing answered. For a TV that's been seen before this is just
			# "off" — most Samsung TVs drop off the network in standby — so it's
			# not an error. For a TV that has *never* answered, the IP may be
			# wrong, so say so (once) and badge the device.
			seen_before = bool(device.states.get('model') or entry.get('everConnected'))
			if retry_count == 0 or entry.get('lastFailureKind') != 'network':
				if seen_before:
					if device.states.get('status') in ('on', 'standby'):
						self.logger.info(f"\"{device.name}\" is off (not responding at {ipaddress})")
					device.setErrorStateOnServer(None)
				else:
					self.logger.warning(
						f"\"{device.name}\": no response from {ipaddress} — the TV is off, or the IP address is wrong. "
						"Turn the TV on and run Plugins → SamsungTV → Run Diagnostics on All TVs if this persists."
					)
					device.setErrorStateOnServer("no response")
			else:
				self.logger.debug(f"\"{device.name}\": still not responding at {ipaddress} (attempt {retry_count + 1}): {detail}")
			entry['lastFailureKind'] = 'network'
			self._setOff(device)
		else:
			# The TV answered, but with something we didn't expect — that's a
			# real problem worth surfacing with the actual detail, not a generic
			# "cannot reach".
			if retry_count == 0 or entry.get('lastFailureKind') != 'unexpected':
				self.logger.error(f"\"{device.name}\": the TV at {ipaddress} answered, but the response couldn't be used: {detail}")
			else:
				self.logger.debug(f"\"{device.name}\": unexpected response again (attempt {retry_count + 1}): {detail}")
			entry['lastFailureKind'] = 'unexpected'
			device.setErrorStateOnServer("error")
		entry['retryCount'] = retry_count + 1

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
		port = int(device.pluginProps.get('restPort') or self._getPort(device))
		rest = samsungtvws.rest.SamsungTVRest(host=ip, port=port, timeout=probe_timeout)

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

	# ---------------------------------------------------------------------------
	# Installed apps (2016+): fetched from the TV itself, like HA's source list
	# ---------------------------------------------------------------------------
	def _installedApps(self, device):
		try:
			return json.loads(device.pluginProps.get('installedApps', '') or '{}')
		except ValueError:
			return {}

	def _refreshAppList(self, devId, announce):
		if devId not in indigo.devices:
			return
		device = indigo.devices[devId]
		try:
			apps = self._getTV(device).app_list()
		except Exception as e:
			if announce:
				self.logger.error(f"Couldn't fetch the app list from \"{device.name}\": {self._describeException(e)[0]}")
			return
		if not apps:
			if announce:
				self.logger.warning(f"\"{device.name}\" didn't return an app list (not all models support it)")
			return
		mapping = {}
		for app in apps:
			name, app_id = app.get('name'), app.get('appId')
			if name and app_id:
				# app_type 4 = native launch; everything else is a deep link.
				mapping[name] = [app_id, "NATIVE_LAUNCH" if str(app.get('app_type')) == "4" else "DEEP_LINK"]
		device = indigo.devices[devId]
		self._updateProps(device, {'installedApps': json.dumps(mapping, sort_keys=True)})
		msg = f"\"{device.name}\": {len(mapping)} installed apps available to Launch App"
		self.logger.info(msg) if announce else self.logger.debug(msg)

	def refreshAppListAction(self, pluginAction, device, callerWaitingForResult=None):
		self._refreshAppList(device.id, True)

	# ---------------------------------------------------------------------------
	# UPnP RenderingControl (volume / mute)
	# ---------------------------------------------------------------------------
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
		try:
			return int(el.text) if el is not None else None
		except (TypeError, ValueError):
			return None

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

	def _refreshVolumeMute(self, device):
		ip = device.pluginProps.get('ipaddress', '')
		volume = self._getVolumeUPnP(ip)
		if volume is not None:
			device.updateStateOnServer("volumeLevel", volume)
		muted = self._getMuteUPnP(ip)
		if muted is not None:
			device.updateStateOnServer("isMuted", muted)
		return volume, muted

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
		device = indigo.devices[device.id]
		props = device.pluginProps
		name = device.name
		ip = props.get('ipaddress', '').strip()
		configured_port = self._getPort(device)
		method_pref = props.get('method', METHOD_AUTO) or METHOD_AUTO

		def line(n, label, ok, detail):
			status = 'OK' if ok is True else ('FAILED' if ok is False else str(ok))
			self.logger.info(f"[{n:<2}] {label:.<44} {status} ({detail})")

		self.logger.info("")
		self.logger.info("{0:=^100}".format(f" Diagnostics: \"{name}\" "))

		# 1. IP address format
		try:
			ipaddress_module.ip_address(ip)
			line(1, "IP address format", True, ip)
		except ValueError:
			line(1, "IP address format", False, f"\"{ip}\" is not a valid IP address")
			self.logger.info("{0:=^100}".format(""))
			self.logger.error(f"\"{name}\": fix the IP address in this device's config before diagnostics can continue")
			return

		# 2. Raw TCP reachability of every port any Samsung protocol uses —
		# bypasses HTTP/TLS/the Tizen API entirely, isolating "is anything
		# there at all" from "does the TV's API respond correctly".
		ports = [
			(WEBSOCKET_SSL_PORT,     "websocket/REST, TLS (2016+)"),
			(WEBSOCKET_NO_SSL_PORT,  "websocket/REST, plain"),
			(ENCRYPTED_PORT,         "encrypted remote (2014-15)"),
			(ENCRYPTED_PAIRING_PORT, "PIN pairing (2014-15)"),
			(LEGACY_PORT,            "legacy remote (pre-2014)"),
			(UPNP_PORT,              "UPnP volume/mute"),
		]
		open_ports = {}
		for i, (port, label) in enumerate(ports):
			ok, detail = self._checkTcpPort(ip, port, timeout=2.0)
			open_ports[port] = ok
			line(f"2{chr(ord('a') + i)}", f"TCP {port} — {label}", ok if ok else "closed", detail)
		any_open = any(open_ports.values())

		# 3. REST device info — what polling depends on
		info, rest_port, rest_detail = None, None, "skipped — neither 8001 nor 8002 is open"
		rest_ports = [p for p in [configured_port, WEBSOCKET_SSL_PORT, WEBSOCKET_NO_SSL_PORT] if open_ports.get(p)]
		if rest_ports:
			try:
				rest_port, info = self._probeRest(ip, list(dict.fromkeys(rest_ports)), 3.0)
				d = info['device']
				rest_detail = f"port {rest_port}: {d.get('name', '?')}, model {d.get('modelName', '?')}"
			except Exception as e:
				rest_detail, _ = self._describeException(e)
		line(3, "Tizen REST API (/api/v2/)", info is not None if rest_ports else "skipped", rest_detail)

		# 4. Power-state reporting
		d = (info or {}).get('device', {})
		if info is not None:
			ps = d.get('PowerState')
			line(4, "PowerState reported", True if ps else "n/a",
				 f"PowerState={ps}" if ps else "not reported by this firmware — on/off is inferred from whether the TV answers")

		# 5. Protocol
		model = d.get('modelName', '') or props.get('model', '')
		if info is not None:
			needs_enc = model_requires_encryption(model)
			if needs_enc:
				detected = METHOD_ENCRYPTED if open_ports.get(ENCRYPTED_PORT) else METHOD_WEBSOCKET
			else:
				detected = METHOD_WEBSOCKET
		elif open_ports.get(LEGACY_PORT):
			detected = METHOD_LEGACY
		else:
			detected = None
		effective = method_pref if method_pref != METHOD_AUTO else (detected or props.get('resolvedMethod') or None)
		proto_detail = f"using {METHOD_LABELS.get(effective, 'unknown')}" + (" (forced in device config)" if method_pref != METHOD_AUTO else " (auto-detected)")
		if detected and method_pref != METHOD_AUTO and detected != method_pref:
			proto_detail += f" — but this TV looks like {METHOD_LABELS[detected]}; consider setting Protocol to Automatic"
		line(5, "Remote-control protocol", effective is not None if any_open else "unknown", proto_detail)

		# 6. Credentials for that protocol
		creds_ok = True
		if effective == METHOD_ENCRYPTED:
			creds_ok = bool(props.get('encToken') and props.get('encSessionId'))
			line(6, "PIN pairing", creds_ok, "paired" if creds_ok else "NOT paired — Edit Device → Pair / Authorize → enter PIN → Submit PIN → Save")
		elif effective == METHOD_WEBSOCKET:
			token_path = self._getTokenPath(device)
			readable = os.path.exists(token_path) and os.access(token_path, os.R_OK | os.W_OK)
			has_token = readable and os.path.getsize(token_path) > 0
			port_in_use = int(props.get('restPort') or configured_port)
			if not readable:
				creds_ok = False
				line(6, "Token file", False, f"{token_path} missing or not writable")
			elif port_in_use == WEBSOCKET_SSL_PORT and not has_token:
				line(6, "Token file", "empty", f"{token_path} — the TV hasn't granted access yet; Edit Device → Pair / Authorize and press Allow on the TV")
			else:
				line(6, "Token file", True, token_path)
		elif effective == METHOD_LEGACY:
			line(6, "Authorization", "n/a", "legacy TVs remember an allowed remote by name; a prompt appears on first use")

		# 7. MAC address stored (required for Wake-on-LAN)
		mac = props.get('macaddress', '') or device.states.get('macaddress', '')
		ag = self._turnOnActionGroup(device)
		if ag:
			line(7, "Turn-on method", True, f"action group \"{ag.name}\"" + (f" (MAC {mac} also known)" if mac else ""))
		else:
			line(7, "MAC address (Wake-on-LAN)", bool(mac), mac or "MISSING — Wake-on-LAN won't work until the TV is seen on once")

		self.logger.info("{0:=^100}".format(""))

		# Plain-language summary of the most likely cause, based on which
		# checks passed — so the log doesn't just dump data, it points
		# somewhere useful.
		if not any_open:
			self.logger.warning(f"\"{name}\": nothing answered at {ip}. If the TV is off, that's normal — most Samsung TVs drop off the network in standby. If it's ON, check that its IP ({ip}) matches the TV's own network settings (DHCP may have changed it).")
		elif info is None and effective != METHOD_LEGACY:
			self.logger.error(f"\"{name}\": the TV accepts connections but its REST API didn't respond properly (see [3]) — likely a TLS/firmware issue, not a wrong IP.")
		elif not creds_ok:
			self.logger.error(f"\"{name}\": reachable, but remote control isn't authorized yet (see [6]).")
		else:
			self.logger.info(f"\"{name}\": diagnostics passed — reachable and controllable via {METHOD_LABELS.get(effective, effective)}.")

	# ---------------------------------------------------------------------------
	# Relay / switch built-in action handlers
	# ---------------------------------------------------------------------------
	def actionControlDevice(self, action, dev):
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			self._reportPowerResult(dev, self.turnOnTV(dev), turning_on=True)
		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			self._reportPowerResult(dev, self.turnOffTV(dev), turning_on=False)
		elif action.deviceAction == indigo.kDeviceAction.Toggle:
			self.toggleTVPower(None, dev)

	def actionControlUniversal(self, action, dev):
		if action.deviceAction == indigo.kUniversalAction.RequestStatus:
			self.logger.info(f"sent \"{dev.name}\" status request")
			self._backgroundPoll(dev)

	# ---------------------------------------------------------------------------
	# Power actions
	# ---------------------------------------------------------------------------
	def toggleTVPower(self, pluginAction, device):
		device = indigo.devices[device.id]
		if device.states.get('status') == "on":
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
			# Only "off" is applied optimistically (the power-off grace window
			# keeps it there). "On" is left to the quick follow-up polls: a TV
			# takes several seconds to boot after Wake-on-LAN — and may not wake
			# at all — so claiming "on" immediately would flicker on→off→on.
			if not turning_on:
				device.updateStatesOnServer([
					{'key': 'onOffState', 'value': False},
					{'key': 'status',     'value': 'off'},
				])
				device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
		elif result == "failed":
			self.logger.error(f"send \"{device.name}\" {verb} failed")
		# "already": turnOnTV()/turnOffTV() already logged an info line — nothing more to do.

	def volumeUpAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeUp(device)

	def volumeDownAction(self, pluginAction, device, callerWaitingForResult=None):
		self._volumeDown(device)

	def _wolTargets(self, device):
		ip = device.pluginProps.get('ipaddress', '').strip()
		override = device.pluginProps.get('wolBroadcast', '').strip()
		if override:
			return [override]
		targets = ["255.255.255.255"]
		try:
			# Directed subnet broadcast too: on a Mac with several interfaces,
			# 255.255.255.255 only leaves via the default route.
			targets.append(str(ipaddress_module.ip_network(f"{ip}/24", strict=False).broadcast_address))
		except ValueError:
			pass
		return targets

	def turnOnTV(self, device):
		"""Returns "sent", "already" (already on — not a failure), or "failed"."""
		device = indigo.devices[device.id]
		entry = self.deviceList.setdefault(device.id, {})
		status = device.states.get('status')
		if status == "on" and not self._powerOffInProgress(entry):
			self.logger.info(f"\"{device.name}\" is already on")
			return "already"
		entry.pop('powerOffUntil', None)

		# A user-supplied turn-on (IR blaster, HDMI-CEC from a receiver, a
		# SmartThings call...) takes precedence over Wake-on-LAN, as in HA.
		ag = self._turnOnActionGroup(device)
		if ag:
			try:
				indigo.actionGroup.execute(ag.id)
				self.logger.info(f"\"{device.name}\": ran turn-on action group \"{ag.name}\"")
				self._schedulePolls(device, [4, 8, 15, 30])
				return "sent"
			except Exception as e:
				self.logger.error(f"\"{device.name}\": turn-on action group \"{ag.name}\" failed: {e} — falling back to Wake-on-LAN")

		sent = False
		# 2018+ TVs in network standby still listen on the websocket — a
		# KEY_POWER there wakes them even where Wake-on-LAN is flaky.
		if status == "standby":
			try:
				self._getTransport(device).send_keys(["KEY_POWER"])
				sent = True
			except Exception as e:
				self.logger.debug(f"KEY_POWER to standby TV \"{device.name}\" failed: {e}")

		macaddress = device.pluginProps.get('macaddress', '') or device.states.get('macaddress', '')
		if macaddress:
			for target in self._wolTargets(device):
				try:
					wakeonlan.send_magic_packet(macaddress, ip_address=target)
					sent = True
				except Exception as m:
					self.logger.debug(f"WOL to {target} for \"{device.name}\" failed: {m}")
			if sent:
				self.logger.info(f"Sent Wake-on-LAN to {macaddress} for \"{device.name}\"")
		if not sent:
			if not macaddress:
				self.logger.error(f"\"{device.name}\": no MAC address stored — turn the TV on manually once so the plugin can learn it, or set a turn-on action group in the device config")
			return "failed"
		self._schedulePolls(device, [4, 8, 15, 30])
		return "sent"

	def turnOffTV(self, device):
		"""Returns "sent", "already" (already off — not a failure), or "failed"."""
		device = indigo.devices[device.id]
		entry = self.deviceList.setdefault(device.id, {})
		if device.states.get('status') not in ("on",) or self._powerOffInProgress(entry):
			self.logger.info(f"\"{device.name}\" is not on")
			return "already"
		self.logger.info(f"Sending power off to \"{device.name}\"")
		try:
			transport = self._getTransport(device)
			transport.power_off(device.pluginProps.get('model', ''))
		except NeedsPairingError:
			self._logNeedsPairing(device)
			return "failed"
		except Exception as m:
			self.logger.error(f"Power off for \"{device.name}\" failed: {self._describeException(m)[0]}")
			self._closeTV(device)
			return "failed"
		entry['powerOffUntil'] = time.monotonic() + POWER_OFF_GRACE
		# Drop the connection so the next command starts fresh.
		self._closeTV(device)
		self._schedulePolls(device, [POWER_OFF_GRACE + 2])
		return "sent"

	# ---------------------------------------------------------------------------
	# Key send helper — single point for all key dispatch, any protocol
	# ---------------------------------------------------------------------------
	def _logNeedsPairing(self, device):
		self.logger.error(
			f"\"{device.name}\" is a 2014-2015 TV that needs one-time PIN pairing: "
			"Edit Device → Pair / Authorize → enter the PIN shown on the TV → Submit PIN → Save"
		)

	def _sendKeys(self, device, keys, label=None, delay=None, repeats=1):
		label = label or ", ".join(keys)
		entry = self.deviceList.setdefault(device.id, {})
		if self._powerOffInProgress(entry):
			self.logger.debug(f"\"{device.name}\" is powering off — not sending {label}")
			return False
		lock = entry.setdefault('sendLock', threading.Lock())
		with lock:
			try:
				transport = self._getTransport(device)
				for _ in range(max(1, repeats)):
					transport.send_keys(keys, delay)
				return True
			except NeedsPairingError:
				self._logNeedsPairing(device)
			except NotSupportedError as e:
				self.logger.error(f"\"{device.name}\": {e}")
			except UnauthorizedError:
				self.logger.error(
					f"\"{device.name}\" refused the remote connection. On the TV: Settings → General → External Device Manager → "
					"Device Connection Manager — allow the remote (or set Access Notification to \"First time only\"), "
					"then Edit Device → Pair / Authorize."
				)
				self._closeTV(device)
			except Exception as m:
				detail = self._describeException(m)[0]
				if self._isNetworkError(m):
					self.logger.error(f"Cannot reach \"{device.name}\" to send {label} — is it on? ({detail})")
				else:
					self.logger.error(f"{label} failed on \"{device.name}\": {detail}")
				self._closeTV(device)
		return False

	def _sendKey(self, device, key, label="key"):
		return self._sendKeys(device, [key], label)

	# ---------------------------------------------------------------------------
	# sendCommand action — remote button dispatch
	# ---------------------------------------------------------------------------
	COMMAND_KEYS = {
		'play':         "KEY_PLAY",
		'pause':        "KEY_PAUSE",
		'stop':         "KEY_STOP",
		'fast_forward': "KEY_FF",
		'rewind':       "KEY_REWIND",
		'channel_up':   "KEY_CHUP",
		'channel_down': "KEY_CHDOWN",
		'previous_channel': "KEY_PRECH",
		'up':           "KEY_UP",
		'down':         "KEY_DOWN",
		'left':         "KEY_LEFT",
		'right':        "KEY_RIGHT",
		'enter':        "KEY_ENTER",
		'back':         "KEY_RETURN",
		'home':         "KEY_HOME",
		'exit':         "KEY_EXIT",
		'menu':         "KEY_MENU",
		'source':       "KEY_SOURCE",
		'guide':        "KEY_GUIDE",
		'tools':        "KEY_TOOLS",
		'info':         "KEY_INFO",
		'channel_list': "KEY_CH_LIST",
		'red':          "KEY_RED",
		'green':        "KEY_GREEN",
		'yellow':       "KEY_YELLOW",
		'blue':         "KEY_CYAN",
	}

	def sendCommand(self, pluginAction, device, callerWaitingForResult=None):
		cmd = pluginAction.props.get("commandCode", "")
		self.logger.debug(f"sendCommand '{cmd}' → {device.name}")

		special = {
			'mute':        self._mute,
			'volume_up':   self._volumeUp,
			'volume_down': self._volumeDown,
			'play_pause':  self._playPause,
		}
		if cmd in special:
			special[cmd](device)
		elif cmd in self.COMMAND_KEYS:
			self._sendKey(device, self.COMMAND_KEYS[cmd], cmd.replace('_', ' ').title())
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
	# sendKeySequence action — several keys, with repeats and inter-key delay
	# (HA's remote.send_command with num_repeats / delay_secs)
	# ---------------------------------------------------------------------------
	def sendKeySequence(self, pluginAction, device, callerWaitingForResult=None):
		raw = pluginAction.props.get("keySequence", "")
		keys = [k.strip().upper() for k in re.split(r"[,\s]+", raw) if k.strip()]
		keys = [k if k.startswith("KEY_") else f"KEY_{k}" for k in keys]
		if not keys:
			self.logger.error("Send Key Sequence: no keys specified")
			return
		try:
			repeats = max(1, min(50, int(pluginAction.props.get("repeats", 1) or 1)))
			delay = max(0.0, min(10.0, float(pluginAction.props.get("delay", 0.5) or 0.5)))
		except ValueError:
			self.logger.error("Send Key Sequence: repeats must be a whole number and delay a number of seconds")
			return
		self.logger.debug(f"Sending {keys} x{repeats} (delay {delay}s) → {device.name}")
		self._sendKeys(device, keys, "key sequence", delay=delay, repeats=repeats)

	def holdKey(self, pluginAction, device, callerWaitingForResult=None):
		key = pluginAction.props.get("keyCode", "").strip().upper()
		if not key:
			self.logger.error("Hold Key: no key code specified")
			return
		try:
			seconds = max(0.1, min(10.0, float(pluginAction.props.get("seconds", 1) or 1)))
		except ValueError:
			self.logger.error("Hold Key: seconds must be a number")
			return
		try:
			self._getTransport(device).hold_key(key, seconds)
		except Exception as e:
			self.logger.error(f"Hold {key} failed on \"{device.name}\": {self._describeException(e)[0]}")

	# ---------------------------------------------------------------------------
	# changeChannel action — digits then Enter (HA's play_media channel)
	# ---------------------------------------------------------------------------
	def changeChannel(self, pluginAction, device, callerWaitingForResult=None):
		channel = pluginAction.props.get("channel", "").strip()
		if not re.fullmatch(r"\d+([-.]\d+)?", channel):
			self.logger.error(f"Change Channel: \"{channel}\" isn't a channel number (e.g. 7 or 7-1)")
			return
		keys = []
		for ch in channel:
			# KEY_PLUS100 is the "-" key used for digital sub-channels (7-1).
			keys.append("KEY_PLUS100" if ch in "-." else f"KEY_{ch}")
		keys.append("KEY_ENTER")
		self.logger.info(f"Changing \"{device.name}\" to channel {channel}")
		self._sendKeys(device, keys, f"channel {channel}", delay=0.3)

	# ---------------------------------------------------------------------------
	# setVolume action — absolute level (0–100)
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

		self.logger.debug(f"UPnP volume control unavailable — stepping {current} → {target} ({diff:+d}) via remote keys on \"{device.name}\"")
		key = "KEY_VOLUP" if diff > 0 else "KEY_VOLDOWN"
		# 100ms per step is still comfortably enough for the TV to register
		# each one; the library default of 1 s would make a 0→100 jump take
		# over a minute and a half.
		if self._sendKeys(device, [key] * abs(diff), "set volume", delay=0.1):
			device.updateStateOnServer("volumeLevel", target)

	def setMute(self, pluginAction, device, callerWaitingForResult=None):
		mode = pluginAction.props.get("muteMode", "toggle")
		ip = device.pluginProps.get('ipaddress', '')
		current = self._getMuteUPnP(ip)
		if mode == "toggle":
			desired = not (current if current is not None else device.states.get("isMuted", False))
		else:
			desired = mode == "on"
		# Explicit on/off via UPnP is idempotent — better than a blind
		# KEY_MUTE toggle, which flips the wrong way if the state was stale.
		if self._setMuteUPnP(ip, desired):
			device.updateStateOnServer("isMuted", desired)
			return
		if current is not None and current == desired:
			return
		if self._sendKey(device, "KEY_MUTE", "Mute"):
			device.updateStateOnServer("isMuted", desired)

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
	# launchApp action — open an app on 2016+ TVs
	# ---------------------------------------------------------------------------
	def _resolveApp(self, device, props):
		app_name = props.get("appName", "")
		if app_name == CUSTOM_APP:
			app_id = props.get("appId", "").strip()
			return app_id, app_id, "DEEP_LINK"
		installed = self._installedApps(device)
		if app_name in installed:
			app_id, app_type = installed[app_name]
			return app_name, app_id, app_type
		if app_name in COMMON_APPS:
			return app_name, COMMON_APPS[app_name], "DEEP_LINK"
		return app_name, None, None

	def launchApp(self, pluginAction, device, callerWaitingForResult=None):
		device = indigo.devices[device.id]
		app_name, app_id, app_type = self._resolveApp(device, pluginAction.props)
		if not app_id:
			self.logger.error(f"launchApp: unknown app '{app_name}'")
			return
		self.logger.info(f"Launching {app_name} on \"{device.name}\"")
		try:
			tv = self._getTV(device)
		except Exception as e:
			self.logger.error(f"Can't launch {app_name} on \"{device.name}\": {self._describeException(e)[0]}")
			return
		try:
			tv.launch_app(app_id, app_type)
		except Exception as ws_err:
			# Some firmware ignores the websocket launch; the REST endpoint
			# is the older fallback.
			self.logger.debug(f"websocket launch of {app_id} failed ({ws_err!r}) — trying REST")
			try:
				port = int(device.pluginProps.get('restPort') or self._getPort(device))
				samsungtvws.rest.SamsungTVRest(host=tv.host, port=port, timeout=self.timeout).rest_app_run(app_id)
			except Exception as m:
				self.logger.error(f"Failed to launch {app_name} on \"{device.name}\": {self._describeException(m)[0]}")
				return
		device.updateStateOnServer("currentApp", app_name)
		device.updateStateOnServer("currentSource", "Smart Hub")

	def getAppList(self, filter="", valuesDict=None, typeId="", targetId=0):
		"""Dynamic list callback for the launchApp action ConfigUI: the TV's
		own installed apps (when known) plus the built-in list."""
		names = []
		if targetId and targetId in indigo.devices:
			names = sorted(self._installedApps(indigo.devices[targetId]).keys(), key=str.lower)
		names += [n for n in COMMON_APPS if n not in names]
		return [(n, n) for n in names] + [(CUSTOM_APP, "Other (enter an app ID)…")]

	def openBrowser(self, pluginAction, device, callerWaitingForResult=None):
		url = pluginAction.props.get("url", "").strip()
		if not url:
			self.logger.error("Open URL: no URL specified")
			return
		try:
			self._getTV(device).open_browser(url)
			self.logger.info(f"Opened {url} on \"{device.name}\"")
		except Exception as e:
			self.logger.error(f"Open URL failed on \"{device.name}\": {self._describeException(e)[0]}")

	def sendText(self, pluginAction, device, callerWaitingForResult=None):
		text = pluginAction.props.get("text", "")
		if not text:
			self.logger.error("Send Text: no text specified")
			return
		try:
			self._getTV(device).send_text(text)
		except Exception as e:
			self.logger.error(f"Send Text failed on \"{device.name}\" (a text field must be focused on the TV): {self._describeException(e)[0]}")

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
			volume, _ = self._refreshVolumeMute(device)
			if volume is None:
				current = device.states.get("volumeLevel", 50)
				device.updateStateOnServer("volumeLevel", min(100, int(current) + 1))

	def _volumeDown(self, device):
		if self._sendKey(device, "KEY_VOLDOWN", "Volume Down"):
			volume, _ = self._refreshVolumeMute(device)
			if volume is None:
				current = device.states.get("volumeLevel", 50)
				device.updateStateOnServer("volumeLevel", max(0, int(current) - 1))

	def _playPause(self, device):
		entry = self.deviceList.setdefault(device.id, {})
		playing = entry.get('playing', False)
		if self._sendKey(device, "KEY_PAUSE" if playing else "KEY_PLAY", "Play/Pause"):
			entry['playing'] = not playing
