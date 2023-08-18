<img src="https://en.wikipedia.org/wiki/Tizen#/media/File:Tizen-Lockup-On-Light-RGB.png" >

[![License](https://img.shields.io/badge/license-MIT-blue.svg?style=flat)](http://mit-license.org)
[![Platform](https://img.shields.io/badge/Platform-Indigo-blueviolet)](https://www.indigodomo.com/) 
[![Language](https://img.shields.io/badge/Language-python%203.10-orange)](https://www.python.org/)
[![Requirements](https://img.shields.io/badge/Requirements-Indigo%20v2022.1%2B-green)](https://www.indigodomo.com/downloads.html)
![Releases](https://img.shields.io/github/release-date/ryanbuckner/samsungtv-plugin?color=red&label=latest%20release)


# SamsungTV Plugin for Indigo Domotics Home Automation
## samsungtv-indigo-plugin for Python3.10
This Indigo Plugin provides a way to connect Indigo to your Samsung TVs with the Tizen OS. This plugin is only supported for [Indigo Domotics Software ](http://www.indigodomo.com)

### What is Tizen?

Tizen is the Operating System on Samsung Smart TVs supported by this plugin

##### Power State Monitoring
Monitor the On/Off state of your TVs

##### Toggle Power
Turn your TVs on and off

##### Custom States
The following custom states are supported 
 TO DO 


### The SamsugtTV Plugin

The plugin is a first MVP to support new models of Samsung TV in Indigo 

More informaton will be listed [on the GitHub Wiki](https://github.com/ryanbuckner/samsungtv-plugin/wiki)

This plugin is not endorsed or associated with Samsung 

##### The plugin currently supports:

1) Support for multiple TVS with Tizon OS 
2) On / Off state monitoring and control 
3) Custom device states of the member device
  - Unique Member ID 
  - Model Number
  - TV Name
  - Connection Type 

#### Installation

Download the SamsungTV.indigoPlugin file and double click it

Required libraries: 
- pip3 install samsungtvws
- pip3 install wakeonlan

Future iterations of this plugin may include the library dependencies

##### Plugin Config 
- configure the Plugin:
  - There are no Plugin config options other than the toggling of Debug

##### Device Config 
- create a new device of Type Samsung TV
  - model should be Samsung TV
  - Name your device anything you want. It's preferred to use something indicating the name or location of the TV
  - Press Edit Device Settings... 
  - Enter the IP address of the TV
  - Enter the port number if it differst from the standard 8002 default 
  - Choose a unique name of the .txt file to save token info (eg 'tv-token.txt')

* Note that the TV must be in developer mode with the IP Address of your Indigo server authorized 
* The TV must be ON to communicate with it


### Future Features:

Future features for this plugin will be community driven. Some ideas: 
1) Launch app action 
2) Send key command action

### Troubleshooting:

- The plugin is unable to communicate with TVs when they are off. It will attempt to connect to the TV and if it cannot after 3 seconds, it makes the assumption that it's Off




