#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2023 ryanbuckner 
# https://github.com/ryanbuckner/samsungtv-plugin/wiki
#

################################################################################
# Imports
################################################################################
import indigo
import re
import sys
import datetime
import samsungtvws
import wakeonlan
import os

################################################################################
# Globals
################################################################################
sys.path.append('../')



################################################################################
class Plugin(indigo.PluginBase):
	########################################
	# Class properties
	########################################



	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.debug = pluginPrefs.get("showDebugInfo", False)
		self.deviceList = []
		self.pluginId = pluginId
		self.pluginprefDirectory = '{}/Preferences/Plugins/com.ryanbuckner.indigoplugin.samsungtv'.format(indigo.server.getInstallFolderPath())
		self.path = self.pluginprefDirectory + "/"
		self.timeout = 3.0

		try:
			self.refresh_frequency = int(self.pluginPrefs.get('refresh_frequency', 30))
			self.logger.debug("Success retrieving preferences from Plugin config")
		except:
			self.logger.error("Error retrieving Plugin preferences. Please use Configure to set")

		self.logger.info(u"")
		self.logger.info(u"{0:=^130}".format("Starting Samsung TV Plugin Engine"))
		self.logger.info(u"{0:<30} {1}".format("Plugin name:", pluginDisplayName))
		self.logger.info(u"{0:<30} {1}".format("Plugin version:", pluginVersion))
		self.logger.info(u"{0:<30} {1}".format("Plugin ID:", pluginId))
		self.logger.info(u"{0:<30} {1}".format("Token Path:", self.path))
		self.logger.info(u"{0:<30} {1}".format("Refresh Frequency:", str(self.refresh_frequency) + " seconds"))
		self.logger.info(u"{0:<30} {1}".format("Indigo version:", indigo.server.version))
		self.logger.info(u"{0:<30} {1}".format("Python version:", sys.version.replace('\n', '')))
		self.logger.info(u"{0:<30} {1}".format("Python Directory:", sys.prefix.replace('\n', '')))
		self.logger.info(u"{0:=^130}".format(""))

		self.samsungtvdata = {}

		try:
			if not os.path.exists(self.pluginprefDirectory):
				os.makedirs(self.pluginprefDirectory)
		except:
			self.logger.error(u'Error accessing or creating tokens folder')
		pass
	########################################
	def deviceStartComm(self, device):
		self.logger.debug("Starting device: " + device.name)
		device.stateListOrDisplayStateIdChanged()

		if device.id not in self.deviceList:
			self.update(device)
			if (device.deviceTypeId == "televisions"):
				self.deviceList.append(device.id)

	########################################
	def deviceStopComm(self, device):
		self.logger.debug("Stopping device: " + device.name)
		if device.id in self.deviceList:
			self.deviceList.remove(device.id)
	
	########################################
	def runConcurrentThread(self):
		self.logger.debug("Starting concurrent thread")
		try:
			pollingFreq = self.refresh_frequency
		except:
			pollingFreq = 30

		self.logger.debug("Current polling frequency is: " + str(pollingFreq) + " seconds")

		# Refresh device states immediately after restarting the Plugin
		iterationcount = 1

		try:
			while True:
				if (iterationcount > 1):
					self.sleep(1 * pollingFreq)
				iterationcount += 1
				self.logger.debug(f"Active devices: {self.deviceList}")
				for deviceId in self.deviceList:
					# call the update method with the device instance
					self.update(indigo.devices[deviceId])
					self.updatedevicestates(indigo.devices[deviceId])
		except self.StopThread:
			pass


	########################################
	def update(self, device):
		#self.logger.debug(device.name)
		device.stateListOrDisplayStateIdChanged()
		return

	########################################
	# UI Validate, Device Config
	########################################
	def validateDeviceConfigUi(self, valuesDict, typeId, device):
		if (typeId == 'televisions'):
			if (not valuesDict['ipaddress']):
				self.logger.error("IP Address is a required field")
				errorsDict = indigo.Dict()
				errorsDict['ipaddress'] = "IP Address  is a required field"
				return (False, valuesDict, errorsDict)

			if (not valuesDict['token']):
				self.logger.error("Token file name is required")
				errorsDict = indigo.Dict()
				errorsDict['token'] = "Token file name is required"
				return (False, valuesDict, errorsDict)

			if (not valuesDict['port']):
				self.logger.error("Port is a required field")
				errorsDict = indigo.Dict()
				errorsDict['port'] = "Port is a required field"
				return (False, valuesDict, errorsDict)

		valuesDict['address'] = valuesDict['ipaddress'];
		file_path = self.path + valuesDict['token']
		if (not os.path.exists(file_path)):
			self.logger.debug("The token at " + file_path + " does not exist, creating")
			try:
				with open(file_path, 'w') as file:
					self.logger.debug(f"File '{file_path}' created successfully.")
			except Exception as e:
				self.logger.debug(f"Error creating file: {e}")

		return (True, valuesDict)


	# assigns the device.address to the value 
	def menuChanged(self, valuesDict = None, typeId = None, devId = None):
		return valuesDict

	def menu_callback(self, valuesDict, *args, **kwargs):  # typeId, devId):
		self.logger.debug(f"Menu callback: returning valuesDict: \n {valuesDict}")
		return valuesDict


	########################################
	# UI Validate, Plugin Preferences
	########################################
	def validatePrefsConfigUi(self, valuesDict):		
		return (True, valuesDict)


	def toggleDebugging(self):
		if self.debug:
			self.debug = False
			self.logger.info(u"Turning off debug logging (Toggle Debugging menu item chosen).")
			self.pluginPrefs['showDebugInfo'] = False
		else:
			self.debug = True
			self.pluginPrefs['showDebugInfo'] = True
			self.logger.debug(u"Turning on debug logging (Toggle Debugging menu item chosen).")



	############################
	# Action Method
	#############################

	def toggleTVPower(self, device):
		if (device.states["onOffState"] == "on"):
			self.turnOffTV(device)
		else:
			self.turnOnTV(device)

		return 


	def turnOffTV(self, device):
		ipaddress = device.pluginProps['ipaddress']
		port = device.pluginProps['port']
		token_file = self.path + device.pluginProps['token'] #device.pluginProps['token_file']
		self.logger.debug(f"Token File: {token_file}")

		if (device.states['status'] == "on"):
			self.logger.debug("Turning off device")
			try:
				tv = samsungtvws.SamsungTVWS(host=ipaddress, port=8002, token_file=token_file, timeout=self.timeout)
				tv.shortcuts().power()
			except Exception as m:
				return False
		else:
			self.logger.info("TV is not on")
			return False

		return True


	def turnOnTV(self, device):
		macaddress = device.states['macaddress']

		if (device.states['status'] == "off"):
			self.logger.debug(f"Turning \"{device.name}\" on")
			try:
				wakeonlan.send_magic_packet(macaddress)
			except Exception as m:
				self.logger.error(f"On cmd for \"{device.name}\" failed")
				return False
		else:
			self.logger.info(f"TV \"{device.name}\" is already on")
			return False

		return True
	

	def sendCommand(self, pluginAction, device, callerWaitingForResult):
		self.logger.debug(f"When creating the Action, here's the device {device.name}")
		#action = str(pluginAction.props.get(u"commandCode"))
		
		match pluginAction.props["commandCode"]:
			case 'mute':
				success = self._mute(device)

			case "volume_up":
				success = self._volumeUp(device)

			case "volume_down":
				success = self._volumeDown(device)

			case "up":
				success = self._up(device)

			case "down":
				success = self._down(device)

			case "left":
				success = self._left(device)

			case "right":
				success = self._right(device)

			case "enter":
				success = self._enter(device)

			case "back":
				success = self._back(device)

			case "home":
				success = self._home(device)

			case "menu":
				success = self._menu(device)

			case "source":
				success = self._source(device)

			case "guide":
				success = self._guide(device)

			case "tools":
				success = self._tools(device)

			case "info":
				success = self._info(device)

		return 

	def _getTV(self, device):
		ipaddress = device.pluginProps['ipaddress']
		port = device.pluginProps['port']
		token_file = self.path + device.pluginProps['token']

		try:
			tv = samsungtvws.SamsungTVWS(host=ipaddress, port=8002, token_file=token_file, timeout=self.timeout)
		except Exception as m:
			self.logger.debug("Failed to connect to TV")
			return False

		return tv


	def _mute(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().mute()
		except Exception as m:
			self.logger.info("Mute call failed")
			return False

		return True

	def _volumeUp(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().volume_up()
		except Exception as m:
			self.logger.info("Volume Up call failed")
			return False

		return True


	def _volumeDown(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().volume_down()
		except Exception as m:
			self.logger.info("Volume Down call failed")
			return False

		return True


	def _up(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().up()
		except Exception as m:
			self.logger.info("Up button call failed")
			return False

		return True


	def _down(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().down()
		except Exception as m:
			self.logger.info("Down button call failed")
			return False

		return True


	def _left(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().left()
		except Exception as m:
			self.logger.info("Left button call failed")
			return False

		return True


	def _right(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().right()
		except Exception as m:
			self.logger.info("Right button call failed")
			return False

		return True


	def _enter(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().enter()
		except Exception as m:
			self.logger.info("Enter button call failed")
			return False

		return True


	def _back(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().back()
		except Exception as m:
			self.logger.info("Back button call failed")
			return False

		return True


	def _home(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().home()
		except Exception as m:
			self.logger.info("Home button call failed")
			return False

		return True


	def _menu(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().menu()
		except Exception as m:
			self.logger.info("Menu button call failed")
			return False

		return True


	def _source(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().source()
		except Exception as m:
			self.logger.info("Source button call failed")
			return False

		return True


	def _guide(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().guide()
		except Exception as m:
			self.logger.info("Guide button call failed")
			return False

		return True


	def _tools(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().tools()
		except Exception as m:
			self.logger.info("Tools button call failed")
			return False

		return True


	def _info(self, device):
		try:
			tv = self._getTV(device)
			tv.shortcuts().info()
		except Exception as m:
			self.logger.info("Info button call failed")
			return False

		return True



	########################################
	# Relay / Dimmer Action callback
	######################
	def actionControlDevice(self, action, dev):
		###### TURN ON ######
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			# Command hardware module (dev) to turn ON here:
			# ** IMPLEMENT ME **
			#send_success = True        # Set to False if it failed.
			send_success = self.turnOnTV(dev)
			if send_success:
				# If success then log that the command was successfully sent.
				self.logger.info(f"sent \"{dev.name}\" on")

				# And then tell the Indigo Server to update the state.
				dev.updateStateOnServer("onOffState", "on")
				dev.updateStateOnServer("status", "on")
			else:
				# Else log failure but do NOT update state on Indigo Server.
				self.logger.error(f"send \"{dev.name}\" on failed")

		###### TURN OFF ######
		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			# Command hardware module (dev) to turn OFF here:
			# ** IMPLEMENT ME **
			send_success = self.turnOffTV(dev)
			#send_success = True        # Set to False if it failed.

			if send_success:
				# If success then log that the command was successfully sent.
				self.logger.info(f"sent \"{dev.name}\" off")

				# And then tell the Indigo Server to update the state:
				dev.updateStateOnServer("onOffState", "off")
				dev.updateStateOnServer("status", "off")
			else:
				# Else log failure but do NOT update state on Indigo Server.
				self.logger.error(f"send \"{dev.name}\" off failed")

	#dump JSON to event log
	def write_json_to_log(self):
		self.logger.debug("TV data has been written to the debug Log. If you did not see it you may need to enable debugging in the Plugin Config UI")
		return


	def refresh_tv_data(self,pluginAction, device):
		self.updatedevicestates(device)
		return


	def status_function_call(tv):
    
	    # token_file = os.path.dirname(os.path.realpath(__file__)) + '/tv-token.txt'
	    # token_file = '/Library/Application Support/Perceptive Automation/Python3-includes/tv-token.txt'
	    #tv = samsungtvws.SamsungTVWS(host='192.168.1.237', port=8002, token_file=token_file)
	    info = tv.rest_device_info()
	    tvState = info['device']['PowerState']

	    return tvState	


	def updatedevicestates(self, device):

		device_states = []
		ipaddress = device.pluginProps['ipaddress']
		port = device.pluginProps['port']
		token_file = self.path + device.pluginProps['token'] #device.pluginProps['token_file']
		tvState = "off"

		self.logger.debug(f"Connecting to device ({device.id}): {device.name}")

		try:
			tv = samsungtvws.SamsungTVWS(host=ipaddress, port=8002, token_file=token_file, timeout=self.timeout)
			info = tv.rest_device_info()
			tvState = info['device']['PowerState'].lower()
			self.logger.debug(f"Power state of {device.name}: {tvState}")
		except Exception as m:
			self.logger.debug(f"Cannot reach device {device.address}. Skipping...")
			device.updateStateOnServer("status", "off")
			device.updateStateOnServer("onOffState", "off")
			device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
			return

		self.logger.debug(f"Capturing all custom states for {device.name}...")
		device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
		device_states.append({'key': 'status','value': tvState })
		device_states.append({'key': 'onOffState','value': 'on' })
		device_states.append({'key': 'name','value': info['device']['name'] })	
		device_states.append({'key': 'os','value': info['device']['OS'] })
		device_states.append({'key': 'macaddress','value': info['device']['wifiMac'] })
		device_states.append({'key': 'device_id','value': info['device']['id'] })
		device_states.append({'key': 'model','value': info['device']['modelName'] })
		device_states.append({'key': 'tokenauthsupport','value': info['device']['TokenAuthSupport'] })
		device_states.append({'key': 'voicesupport','value': info['device']['VoiceSupport'] })
		device_states.append({'key': 'firmwareversion','value': info['device']['firmwareVersion'] })
		device_states.append({'key': 'networktype','value': info['device']['networkType'] })
		device_states.append({'key': 'type','value': info['type']})
		
		device.updateStatesOnServer(device_states)

		return





	