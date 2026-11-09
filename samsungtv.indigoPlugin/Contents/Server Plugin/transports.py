#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Connection "transports" for the three Samsung remote-control protocols.

Samsung has shipped three incompatible remote protocols over the years. This
mirrors the model Home Assistant's samsungtv integration uses
(homeassistant/components/samsungtv/bridge.py), so every TV HA can drive,
this plugin can drive too:

  websocket  — 2016+ Tizen TVs (K series onward). JSON over a websocket on
               8002 (TLS, token auth) or 8001 (plain). REST device info on
               the same port.
  encrypted  — 2014–2015 H and J series. AES-encrypted websocket on port
               8000, after a one-time PIN pairing (PIN shown on the TV,
               pairing endpoint on port 8080). These TVs still answer the
               REST device-info call on 8001/8002 but reject plain-websocket
               key presses.
  legacy     — 2013 and earlier (C/D/E/F series). Binary protocol on port
               55000; the TV shows an "Allow" prompt on first connection.

Every transport exposes the same small interface (send_keys, power_off,
close, ...) so the plugin can dispatch without caring which protocol a TV
speaks.
"""

import asyncio
import logging
import threading

import samsungtvws
from samsungtvws.exceptions import UnauthorizedError

# The encrypted and legacy protocols are async-only in samsungtvws and need
# its [async,encrypted] extras (aiohttp, websockets, cryptography,
# py3rijndael). Import them defensively so a missing extra only disables the
# protocols that need it instead of taking the whole plugin down.
try:
	import aiohttp
	from samsungtvws.encrypted.authenticator import SamsungTVEncryptedWSAsyncAuthenticator
	from samsungtvws.encrypted.remote import SamsungTVEncryptedWSAsyncRemote, SendRemoteKey as EncryptedKey
	from samsungtvws.legacy.remote import SamsungTVLegacyAsyncRemote
	ASYNC_IMPORT_ERROR = None
except Exception as _err:  # pragma: no cover - depends on installed extras
	ASYNC_IMPORT_ERROR = _err

LOGGER = logging.getLogger("Plugin.transports")

METHOD_AUTO      = "auto"
METHOD_WEBSOCKET = "websocket"
METHOD_ENCRYPTED = "encrypted"
METHOD_LEGACY    = "legacy"

WEBSOCKET_SSL_PORT    = 8002
WEBSOCKET_NO_SSL_PORT = 8001
ENCRYPTED_PORT        = 8000
ENCRYPTED_PAIRING_PORT = 8080
LEGACY_PORT           = 55000

# Remote name shown on the TV's "Allow this device?" prompt / Device
# Connection Manager. The websocket transport deliberately keeps the
# samsungtvws default ("SamsungTvRemote") so TVs already authorized under that
# name don't re-prompt after an upgrade.
LEGACY_REMOTE_NAME = "Indigo"

# Per-model power-off keys for encrypted (H/J) TVs, from HA's bridge.py.
# Keyed by the model name minus its 4-character size/region prefix
# (UN48JU6400 -> JU6400). Unknown models get both keys, as HA does.
ENCRYPTED_MODEL_USES_POWER_OFF = {"H6400", "H6410"}
ENCRYPTED_MODEL_USES_POWER     = {"JU6400", "JU641D"}


class NeedsPairingError(Exception):
	"""Raised when an encrypted-protocol TV has no stored PIN-pairing credentials."""


class NotSupportedError(Exception):
	"""Raised when a feature isn't available over this TV's protocol."""


def model_requires_encryption(model):
	"""2014-2015 H and J models need the encrypted protocol + PIN pairing.
	Same rule as HA: the 5th character of the model name (UN48JU6400 -> 'J')."""
	return bool(model) and len(model) > 4 and model[4] in ("H", "J")


def short_model(model):
	return model[4:] if model and len(model) > 4 else ""


# ---------------------------------------------------------------------------
# A single background asyncio loop shared by every async transport. Indigo
# plugin callbacks are plain threads; run_coroutine_threadsafe() lets them
# call the async-only samsungtvws APIs synchronously, and running everything
# on one loop thread also serializes access to each TV's connection.
# ---------------------------------------------------------------------------
class AsyncRunner:
	def __init__(self):
		self._loop = None
		self._thread = None
		self._session = None
		self._lock = threading.Lock()

	@property
	def available(self):
		return ASYNC_IMPORT_ERROR is None

	def _ensure_started(self):
		with self._lock:
			if self._loop is not None:
				return
			loop = asyncio.new_event_loop()
			thread = threading.Thread(target=loop.run_forever, name="SamsungTV-async", daemon=True)
			thread.start()
			self._loop, self._thread = loop, thread

	def run(self, coro, timeout=40):
		if not self.available:
			coro.close()
			raise NotSupportedError(
				f"the samsungtvws async/encrypted extras failed to import ({ASYNC_IMPORT_ERROR}) — "
				"2014-2015 H/J and pre-2014 TVs can't be controlled until the plugin's requirements install cleanly"
			)
		self._ensure_started()
		future = asyncio.run_coroutine_threadsafe(coro, self._loop)
		try:
			return future.result(timeout)
		except Exception:
			future.cancel()
			raise

	async def session(self):
		"""Shared aiohttp session — must be created/used on the loop thread."""
		if self._session is None or self._session.closed:
			self._session = aiohttp.ClientSession()
		return self._session

	def stop(self):
		with self._lock:
			loop, self._loop = self._loop, None
		if loop is None:
			return
		async def _close_session():
			if self._session is not None and not self._session.closed:
				await self._session.close()
		try:
			asyncio.run_coroutine_threadsafe(_close_session(), loop).result(5)
		except Exception:
			pass
		loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# Websocket (2016+ Tizen)
# ---------------------------------------------------------------------------
class WebsocketTransport:
	method = METHOD_WEBSOCKET
	supports_apps = True
	supports_hold = True
	supports_text = True

	def __init__(self, host, port, token_file, timeout=3.0, frame_tv=False):
		self.host = host
		self.port = port
		self.token_file = token_file
		self.timeout = timeout
		self.frame_tv = frame_tv
		self._tv = None
		self._lock = threading.RLock()

	def _get(self, force_new=False):
		if force_new:
			self.close()
		if self._tv is None:
			self._tv = samsungtvws.SamsungTVWS(
				host=self.host, port=self.port, token_file=self.token_file, timeout=self.timeout
			)
		return self._tv

	def _call(self, fn):
		"""Run fn(tv), reconnecting once if the cached connection went stale
		(TV rebooted, went to standby, etc.). Auth refusals are not retried."""
		with self._lock:
			try:
				return fn(self._get())
			except UnauthorizedError:
				self.close()
				raise
			except Exception as first:
				LOGGER.debug(f"websocket call to {self.host} failed ({first!r}) — reconnecting and retrying")
				return fn(self._get(force_new=True))

	def send_keys(self, keys, delay=None):
		def _send(tv):
			for i, key in enumerate(keys):
				# No trailing delay after the final key — samsungtvws defaults to a
				# full second after every press, which makes one-shot commands lag.
				d = 0 if i == len(keys) - 1 else delay
				tv.send_key(key, key_press_delay=d)
		self._call(_send)

	def hold_key(self, key, seconds):
		self._call(lambda tv: tv.hold_key(key, seconds))

	def power_off(self, model=None):
		# Frame TVs treat a short KEY_POWER press as "go to Art Mode"; a 3 s
		# hold is a real power-off (same as HA).
		if self.frame_tv:
			self.hold_key("KEY_POWER", 3)
		else:
			self.send_keys(["KEY_POWER"])

	def launch_app(self, app_id, app_type="DEEP_LINK", meta_tag=""):
		self._call(lambda tv: tv.run_app(app_id, app_type, meta_tag))

	def open_browser(self, url):
		self._call(lambda tv: tv.open_browser(url))

	def send_text(self, text):
		self._call(lambda tv: tv.send_text(text))

	def app_list(self):
		with self._lock:
			try:
				return self._get().app_list()
			except Exception as e:
				LOGGER.debug(f"app list from {self.host} failed: {e!r}")
				self.close()
				return None

	def close(self):
		with self._lock:
			tv, self._tv = self._tv, None
		if tv is not None:
			try:
				tv.close()
			except Exception:
				pass


# ---------------------------------------------------------------------------
# Encrypted (2014-2015 H/J series)
# ---------------------------------------------------------------------------
class EncryptedTransport:
	method = METHOD_ENCRYPTED
	supports_apps = False
	supports_hold = False
	supports_text = False

	def __init__(self, runner, host, token, session_id, model=None, port=ENCRYPTED_PORT, timeout=5.0):
		self.runner = runner
		self.host = host
		self.port = port
		self.token = token
		self.session_id = session_id
		self.model = model
		self.timeout = timeout
		self._remote = None

	async def _get_remote(self, force_new=False):
		if force_new:
			await self._aclose()
		if self._remote is None or not self._remote.is_alive():
			remote = SamsungTVEncryptedWSAsyncRemote(
				host=self.host, port=self.port,
				web_session=await self.runner.session(),
				token=self.token, session_id=self.session_id,
				timeout=self.timeout,
			)
			await remote.start_listening()
			self._remote = remote
		return self._remote

	async def _asend(self, keys, delay):
		commands = [EncryptedKey.click(k) for k in keys]
		for attempt in (0, 1):
			try:
				remote = await self._get_remote(force_new=attempt > 0)
				await remote.send_commands(commands, key_press_delay=delay)
				return
			except Exception as e:
				if attempt:
					raise
				LOGGER.debug(f"encrypted send to {self.host} failed ({e!r}) — reconnecting and retrying")

	def send_keys(self, keys, delay=None):
		if not (self.token and self.session_id):
			raise NeedsPairingError()
		self.runner.run(self._asend(list(keys), 0.4 if delay is None else delay))

	def hold_key(self, key, seconds):
		raise NotSupportedError("press-and-hold isn't supported by the encrypted (2014-2015) protocol")

	def power_off(self, model=None):
		sm = short_model(model or self.model)
		if sm in ENCRYPTED_MODEL_USES_POWER_OFF:
			keys = ["KEY_POWEROFF"]
		elif sm in ENCRYPTED_MODEL_USES_POWER:
			keys = ["KEY_POWER"]
		else:
			keys = ["KEY_POWEROFF", "KEY_POWER"]
		self.send_keys(keys)

	async def _aclose(self):
		remote, self._remote = self._remote, None
		if remote is not None:
			try:
				await remote.close()
			except Exception:
				pass

	def close(self):
		if self._remote is None:
			return
		try:
			self.runner.run(self._aclose(), timeout=5)
		except Exception:
			self._remote = None


class EncryptedPairing:
	"""One in-progress PIN pairing. start() makes the TV show a PIN; finish(pin)
	exchanges it for the (token, session_id) pair the encrypted remote needs."""

	def __init__(self, runner, host):
		self.runner = runner
		self.host = host
		self._auth = None

	# These run inside Indigo config-dialog button callbacks, which must return
	# within a few seconds, so callers pass a short overall timeout. The
	# per-HTTP-request timeout is kept below it.
	async def _astart(self, request_timeout):
		self._auth = SamsungTVEncryptedWSAsyncAuthenticator(
			self.host, web_session=await self.runner.session(), timeout=request_timeout
		)
		await self._auth.start_pairing()

	def start(self, timeout=6):
		self.runner.run(self._astart(max(1.0, timeout / 2)), timeout=timeout)

	async def _afinish(self, pin):
		token = await self._auth.try_pin(pin)
		if not token:
			return None, None
		session_id = await self._auth.get_session_id_and_close()
		return token, session_id

	def finish(self, pin, timeout=6):
		if self._auth is None:
			raise RuntimeError("pairing was not started")
		return self.runner.run(self._afinish(pin), timeout=timeout)


# ---------------------------------------------------------------------------
# Legacy (2013 and earlier)
# ---------------------------------------------------------------------------
class LegacyTransport:
	method = METHOD_LEGACY
	supports_apps = False
	supports_hold = False
	supports_text = False

	def __init__(self, runner, host, port=LEGACY_PORT, timeout=5.0):
		self.runner = runner
		self.host = host
		self.port = port
		self.timeout = timeout
		self._remote = None

	def _new_remote(self, timeout):
		return SamsungTVLegacyAsyncRemote(
			self.host, port=self.port,
			name=LEGACY_REMOTE_NAME, description=LEGACY_REMOTE_NAME, remote_id=LEGACY_REMOTE_NAME,
			timeout=timeout,
		)

	async def _get_remote(self, force_new=False):
		if force_new:
			await self._aclose()
		if self._remote is None or not self._remote.is_connected:
			remote = self._new_remote(self.timeout)
			await remote.open()
			self._remote = remote
		return self._remote

	async def _asend(self, keys, delay):
		for i, key in enumerate(keys):
			if i:
				await asyncio.sleep(delay)
			for attempt in (0, 1):
				try:
					remote = await self._get_remote(force_new=attempt > 0)
					await remote.control(key)
					break
				except UnauthorizedError:
					raise
				except Exception as e:
					if attempt:
						raise
					LOGGER.debug(f"legacy send to {self.host} failed ({e!r}) — reconnecting and retrying")

	def send_keys(self, keys, delay=None):
		# Legacy TVs drop presses sent faster than ~1 s apart (HA uses 1.2 s).
		self.runner.run(self._asend(list(keys), 1.2 if delay is None else delay),
						timeout=10 + 2 * len(keys))

	def hold_key(self, key, seconds):
		raise NotSupportedError("press-and-hold isn't supported by the legacy (pre-2014) protocol")

	def power_off(self, model=None):
		self.send_keys(["KEY_POWEROFF"])

	async def _aauthorize(self, timeout):
		remote = self._new_remote(timeout)
		try:
			await remote.open()
		finally:
			await remote.close()

	def authorize(self, timeout=30):
		"""Open a connection with a long timeout so the user has time to press
		Allow on the TV. Raises UnauthorizedError if they press Deny."""
		self.runner.run(self._aauthorize(timeout), timeout=timeout + 5)

	async def _aclose(self):
		remote, self._remote = self._remote, None
		if remote is not None:
			try:
				await remote.close()
			except Exception:
				pass

	def close(self):
		if self._remote is None:
			return
		try:
			self.runner.run(self._aclose(), timeout=5)
		except Exception:
			self._remote = None
