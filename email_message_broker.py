import imaplib
import email
import json
import re
import yaml
import importlib
import requests
import subprocess
import mysql.connector
import logging
from logging.handlers import RotatingFileHandler
from email.policy import default
import paho.mqtt.publish as mqtt_publish

# Logging-Konfiguration
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler = RotatingFileHandler('email_message_broker.log', maxBytes=5*1024*1024, backupCount=3)
log_handler.setFormatter(log_formatter)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(log_handler)

# Laden der Konfigurationsdateien
try:
    with open('services.yaml', 'r') as file:
        services_config = yaml.safe_load(file)
except FileNotFoundError:
    logger.error("Konfigurationsdatei 'services.yaml' nicht gefunden.")
    services_config = {}

try:
    with open('config.yaml', 'r') as file:
        config = yaml.safe_load(file)
except FileNotFoundError:
    logger.error("Konfigurationsdatei 'config.yaml' nicht gefunden.")
    config = {}

mqtt_config = config.get('mqtt', {})
db_config = config.get('database', {})
imap_config = config.get('imap', {})

IMAP_SERVER = imap_config.get('server')
IMAP_USER = imap_config.get('user')
IMAP_PASS = imap_config.get('password')
WHITELIST = imap_config.get('whitelist', [])

def load_function(function_name):
    module = importlib.import_module('device_actions')
    function = getattr(module, function_name, None)
    if not function:
        logger.error(f"Funktion '{function_name}' in 'device_actions' nicht gefunden.")
    return function

def parse_body(body):
    try:
        params = json.loads(body)
        if isinstance(params, dict):
            logger.info('E-Mail-Body als JSON erkannt und verarbeitet.')
            return params
    except json.JSONDecodeError:
        logger.warning('JSON-Parsing fehlgeschlagen, versuche Key-Value-Format.')
    
    params = {}
    for line in body.splitlines():
        match = re.match(r'(\w+)\s*:\s*(.+)', line)
        if match:
            key, value = match.groups()
            params[key.strip()] = value.strip()
    logger.info('E-Mail-Body als Key-Value erkannt und verarbeitet.')
    return params

def extract_priority(subject):
    match = re.search(r'\[PRIORITY:(\d+)\]', subject)
    try:
        return int(match.group(1)) if match else float('inf')
    except ValueError:
        logger.warning(f"Ungültige Priorität im Betreff: {subject}")
        return float('inf')

def validate_and_call_service(action, params):
    service = services_config['services'].get(action)
    
    if service:
        required_params = {p['name'] for p in service['parameters'] if p.get('required', False)}
        missing_params = required_params - params.keys()
        
        if missing_params:
            logger.error(f"Fehlende Parameter für {action}: {', '.join(missing_params)}")
            return

        if service['type'] == 'function':
            function_name = service['function']
            function = load_function(function_name)
            if function:
                function(params)
                logger.info(f"Aktion '{action}' erfolgreich als lokale Funktion mit Parametern {params} ausgeführt.")
            else:
                logger.error(f"Funktion '{function_name}' nicht gefunden.")
        
        elif service['type'] == 'http':
            method = service.get('method', 'GET').upper()
            url = service['url']
            try:
                if method == 'POST':
                    response = requests.post(url, json=params, timeout=10)
                else:
                    response = requests.get(url, params=params, timeout=10)
                
                if response.ok:
                    logger.info(f"HTTP-Aufruf für '{action}' erfolgreich. Status: {response.status_code}, Antwort: {response.text}")
                else:
                    logger.error(f"Fehler bei HTTP-Aufruf für '{action}'. Status: {response.status_code}, Antwort: {response.text}")
            except requests.RequestException as e:
                logger.error(f"HTTP-Aufruf für '{action}' fehlgeschlagen: {e}")

        elif service['type'] == 'shell':
            command = service['command']
            execute_shell_command(command, params)
        
        elif service['type'] == 'database':
            query = service['query']
            execute_database_query(query, params)
        
        elif service['type'] == 'mqtt':
            topic = service['topic']
            publish_mqtt_message(topic, params)

    else:
        logger.warning(f"Unbekannte Aktion: '{action}'")

def execute_shell_command(command, params):
    allowed_commands = ['reboot', 'shutdown']
    if any(cmd not in allowed_commands for cmd in command.split()):
        logger.error(f"Befehl '{command}' ist nicht erlaubt.")
        return

    try:
        full_command = [command] + [str(value) for value in params.values()]
        result = subprocess.run(full_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info(f"Shell-Befehl erfolgreich: {result.stdout.decode()}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Fehler beim Ausführen des Shell-Befehls: {e.stderr.decode()}")

def execute_database_query(query, params):
    try:
        connection = mysql.connector.connect(
            host=db_config.get('host', 'localhost'),
            user=db_config.get('user'),
            password=db_config.get('password'),
            database=db_config.get('database')
        )
        cursor = connection.cursor(prepared=True)
        cursor.execute(query, params)
        connection.commit()
        logger.info("Datenbank-Abfrage erfolgreich ausgeführt.")
    except mysql.connector.Error as e:
        logger.error(f"Fehler bei Datenbank-Abfrage: {e}")
    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()

def publish_mqtt_message(topic, params):
    try:
        message = json.dumps(params)
        mqtt_auth = None
        if mqtt_config.get('username'):
            mqtt_auth = {
                'username': mqtt_config.get('username'),
                'password': mqtt_config.get('password')
            }
        mqtt_publish.single(
            topic,
            message,
            hostname=mqtt_config.get('hostname', 'localhost'),
            port=mqtt_config.get('port', 1883),
            auth=mqtt_auth if mqtt_auth and mqtt_auth['username'] else None
        )
        logger.info(f"MQTT-Nachricht erfolgreich auf Topic '{topic}' veröffentlicht.")
    except Exception as e:
        logger.error(f"Fehler beim Veröffentlichen der MQTT-Nachricht: {e}")

def fetch_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select('inbox')
        logger.info('IMAP-Server verbunden und Postfach ausgewählt.')

        status, response = mail.search(None, '(UNSEEN)')
        email_ids = response[0].split()

        emails = []

        for email_id in email_ids:
            status, data = mail.fetch(email_id, '(RFC822)')
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email, policy=default)
            
            sender = msg['From']
            sender_email = email.utils.parseaddr(sender)[1]
            subject = msg['Subject']

            if sender_email in WHITELIST:
                priority = extract_priority(subject)
                action_match = re.match(r'(\w+)', subject)
                if action_match:
                    action = action_match.group(1)
                    body = get_email_body(msg)
                    params = parse_body(body)
                    emails.append((priority, email_id, action, params))
                else:
                    logger.warning('Betreff konnte nicht analysiert werden.')
            else:
                logger.warning(f'Unerlaubter Absender: {sender_email}')

            mail.store(email_id, '+FLAGS', '\\Seen')

        emails.sort(key=lambda x: x[0])

        for _, email_id, action, params in emails:
            validate_and_call_service(action, params)
        
        mail.logout()
        logger.info('IMAP-Verbindung geschlossen.')

    except Exception as e:
        logger.error(f'Fehler beim Abrufen der E-Mails: {e}')

def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.iter_parts():
            if part.get_content_type() == 'text/plain':
                return part.get_payload(decode=True).decode()
    else:
        return msg.get_payload(decode=True).decode()
    return ""

if __name__ == "__main__":
    logger.info('Starte E-Mail Message-Broker.')
    fetch_emails()
    logger.info('E-Mail Message-Broker beendet.')
