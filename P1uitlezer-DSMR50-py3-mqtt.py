#!/usr/bin/python3
#
# DSMR P1 uitlezer
# (c) 10-2012 2016 - GJ - gratis te kopieren en te plakken
#
# Tweaks and fixes by Florian Overkamp
# Based on original sources by Ge Jansen and Python3 fixes (and more) by Stas Zytkiewicz
# Reference: https://www.netbeheernederland.nl/_upload/Files/Slimme_meter_15_a727fce1f1.pdf
#
from datetime import datetime
from pprint import pprint
import configparser
import random
import json
import paho.mqtt.client as mqtt
import datetime

versie = "1.2-py3"
import sys
import serial


################
# Error display 
################
def show_error(mesg=''):
    ft = sys.exc_info()[0]
    fv = sys.exc_info()[1]
    print("Fout in %s type: %s" % (mesg, ft))
    print("Fout waarde: %s" % fv)
    return

def halt(mesg="Clean exit", ret=0):
    print(mesg)
    # Close port and show status
    try:
        ser.close()
    except:
        show_error(ser.name)
        # in *nix you should pass 0 for succes or >0 when it fails as exitr value
        sys.exit(1)
    sys.exit(ret)


##################################################
# Main program
##################################################
# Read config if available
config = configparser.ConfigParser()
config.read('p1dsmr.ini')

# Set COM port config
ser = serial.Serial()
ser.port = config.get('general', 'p1port', fallback='/dev/ttyUSB0')
ser.baudrate = config.get('general', 'p1speed', fallback='115200')
ser.bytesize = serial.SEVENBITS
ser.parity = serial.PARITY_EVEN
ser.stopbits = serial.STOPBITS_ONE
ser.xonxoff = 0
ser.rtscts = 0
ser.timeout = 20

# Print program banner
print("DSMR 5.0 P1 uitlezer", versie)

# Open COM port
try:
    ser.open()
except:
    show_error(ser.name)
    halt("Error opening serial socket", ret=1)

# Initialize
# stack is mijn list met de 36 regeltjes.
t_lines = {}
# Test
# with open('test-telegram.txt', 'r') as f:
#     lines = f.readlines()

##################################################
# Read one full telegram from serial port
##################################################
# Telegram start/end markers
p1_start = False
p1_end = False

while not p1_end:
    p1_line = ''
    # Read 1 line
    try:
        p1 = ser.readline()
        # if lines:
        #     p1 = lines.pop(0)
        # else:
        #     break
    except Exception as e:
        print(e)
        show_error(ser.name)
        halt("Error reading serial socket", ret=2)
    else:
        p1 = p1.decode()

    #print("raw output", p1)
    if p1.startswith("/"):
        # at start of telegram, clear buffers
        p1_start = True
        t_lines = {}
        print("Telegram start:", p1.strip());

    if p1[0].isdigit():
        #print(p1)
        key, val = p1.strip().split('(', 1)
        if "1-0:99.97.0" in key:
            # special case with possible powerfailures list
            t_lines[key] = val
            continue
        if "0-1:24.2.1" in key:
            # remove timestamp from the gas meter
            tstamp, usage = val.split('(', 1)
            usage = usage.split('*m3')[0]
            t_lines[key] = usage
            continue
        val = val[:-1] # loose last )
        if "*kW" in val:
            val = val.split('*kW')[0]
        t_lines[key] = val
    elif p1.startswith("!"):
        # at end of telegram. might check CRC?
        if p1_start:
            p1_end = True
            print("Telegram end:", p1.strip());
            break
# end while

print(len(t_lines), "keys read")


##################################################
# Printout 
##################################################
# Create a nice printout and build a structured array to save to mqtt or similar
meter = 0
dsmr = {}

for key, val in t_lines.items():
    if key == "1-0:1.8.1":
        print("{:<30s} {:<10} KW".format("totaal laagtarief verbruik", val))
        dsmr["t1usage"] = float(val)
        meter += int(float(val))
    elif key == "1-0:1.8.2":
        print("{:<30s} {:<10} KW".format("totaal hoogtarief verbruik", val))
        dsmr["t2usage"] = float(val)
        meter += int(float(val))
    elif key == "1-0:2.8.1":
        print("{:<30s} {:<10} KW".format("totaal laagtarief retour", val))
        dsmr["t1return"] = float(val)
        meter -= int(float(val))
    elif key == "1-0:2.8.2":
        print("{:<30s} {:<10} KW".format("totaal hoogtarief retour", val))
        dsmr["t2return"] = float(val)
        meter -= int(float(val))
    elif key == "1-0:1.7.0":
        print("{:<30s} {:<10} W".format("huidig afgenomen vermogen", float(val) * 1000))
        dsmr["consuming"] = float(val)*1000
    elif key == "1-0:2.7.0":
        print("{:<30s} {:<10} W".format("huidig teruggeleverd vermogen", float(val) * 1000))
        dsmr["returning"] = float(val)*1000
    elif key == "0-1:24.2.1":
# Please note: This *assumes* the gas meter is connected to M-bus channel 1. 
# This might not be the case in your setup. It could also be water or thermal ('stadsverwarming')
# Future improvement idea: make this configurable :-)
        print("{:<30s} {:<10} m3".format("totaal gas verbruik", val))
        dsmr["gasusage"] = float(val)
    elif key == "0-0:1.0.0":
        timestamp = val[0:12]
        if(val[12] == 'W'):
            timezone = '+0100'
        elif(val[12] == 'S'):
            timezone = '+0200'
        else:
            print('Ongeldige DST vlag - wtf?')
        fulltimestamp = timestamp + timezone
        timestamp = datetime.datetime.strptime(fulltimestamp, '%y%m%d%H%M%S%z')
        print("Timestamp van de meter:", timestamp)
        dsmr["time"] = int(timestamp.timestamp())

print("{:<30s} {:<10} KW {:<10}".format("meter totaal", meter, "afgenomen/teruggeleverd van het net"))

##################################################
# MQTT section
##################################################
# Publish to MQTT

# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
    print("Connected with result code "+str(rc))

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    client.subscribe("$SYS/#")

# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
    print(msg.topic+" "+str(msg.payload))

if config.get('mqtt', 'enable', fallback='false').lower() == 'true':
    print('Publishing to MQTT')
    broker = config.get('mqtt', 'hostname')
    port = int(config.get('mqtt', 'port', fallback='1883'))
    username = config.get('mqtt', 'username', fallback='')
    password = config.get('mqtt', 'password', fallback='')
    topic = config.get('mqtt', 'topic')
    client_id = f'p1psrm-mqtt-{random.randint(0, 1000)}'
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    if username != '' and password != '':
        client.username_pw_set(username, password)
    client.connect(broker, port, 60)

    client.publish(topic, json.dumps(dsmr))

