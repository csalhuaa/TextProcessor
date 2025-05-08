import threading
from time import sleep
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
import amqpstorm
from amqpstorm import Message
import os
import datetime
import json
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuración de CloudAMQP
CLOUDAMQP_URL = os.environ.get('CLOUDAMQP_URL', 'amqps://tnluigbk:x9gWN83qzJ3CIZjiKKAyg327wKNb9eA1@porpoise.rmq.cloudamqp.com/tnluigbk')
url = urlparse(CLOUDAMQP_URL)

server_started = False

# Extraer componentes de la URL
RABBIT_HOST = url.hostname
RABBIT_USER = url.username
RABBIT_PASSWORD = url.password
RABBIT_VHOST = url.path[1:] if url.path else '%2f'
RABBIT_PORT = 5671  # Puerto para TLS
RABBIT_SSL = True   # Habilitar SSL para conexión segura
RPC_QUEUE = 'rpc_queue'

# Estado global del servidor RPC
SERVER_STATUS = {
    "running": False,
    "processed_messages": 0,
    "errors": 0
}

class TextProcessingServer(object):
    """Servidor RPC para operaciones de procesamiento de texto."""
    
    def __init__(self, host, username, password, rpc_queue, vhost, port=5671, ssl=True):
        self.host = host
        self.username = username
        self.password = password
        self.rpc_queue = rpc_queue
        self.vhost = vhost
        self.port = port
        self.ssl = ssl
        self.connection = None
        self.channel = None
        
    def start(self):
        """Iniciar el servidor RPC con reconexión automática."""
        while True:
            try:
                # Imprimir información de conexión
                print(f"Usuario: {self.username}, vhost: {self.vhost}")
                
                # Crear conexión con RabbitMQ
                self.connection = amqpstorm.Connection(
                    self.host,
                    self.username,
                    self.password,
                    virtual_host=self.vhost,
                    port=self.port,
                    ssl=self.ssl,
                    heartbeat=30  # Configurar heartbeat más frecuente
                )
                self.channel = self.connection.channel()
                
                # Declarar la cola RPC
                self.channel.queue.declare(self.rpc_queue)
                
                # Configurar calidad de servicio (QoS)
                self.channel.basic.qos(prefetch_count=1)
                
                # Iniciar consumo de mensajes
                self.channel.basic.consume(self._process_request, self.rpc_queue)
                
                print(f"[x] Servidor de Procesamiento de Texto iniciado. Esperando mensajes en la cola '{self.rpc_queue}'...")
                SERVER_STATUS["running"] = True
                
                # Comenzar a consumir mensajes
                self.channel.start_consuming()
                
            except amqpstorm.exception.AMQPConnectionError as e:
                print(f"Conexión perdida: {str(e)}")
                SERVER_STATUS["errors"] += 1
                SERVER_STATUS["running"] = False
                print("Intentando reconectar en 5 segundos...")
                import time
                time.sleep(5)
            except Exception as e:
                print(f"Error crítico en el servidor RPC: {str(e)}")
                SERVER_STATUS["errors"] += 1
                SERVER_STATUS["running"] = False
                raise
            
    def _process_request(self, message):
        """Procesar solicitudes RPC entrantes."""
        try:
            # Obtener el contenido del mensaje
            payload = message.body
            
            # Procesar el texto
            print(f"[.] Solicitud recibida: {payload}")
            response = self._process_text(payload)
            
            # Crear mensaje de respuesta
            properties = {
                'correlation_id': message.correlation_id
            }
            response_message = Message.create(
                message.channel, 
                response,
                properties
            )
            
            # Enviar respuesta
            response_message.publish(message.reply_to)
            
            # Confirmar procesamiento del mensaje
            message.ack()
            
            print(f"[x] Respuesta enviada: {response}")
            SERVER_STATUS["processed_messages"] += 1
            
        except Exception as e:
            print(f"Error al procesar solicitud: {str(e)}")
            message.nack()
            SERVER_STATUS["errors"] += 1
        
    def _process_text(self, payload):
        """Procesar el texto según el comando proporcionado."""
        # Formato esperado: "comando:texto"
        try:
            # Dividir comando y texto
            command, text = payload.split(':', 1)
            
            # Aplicar operación de texto
            if command == "mayusculas":
                return text.upper()
            elif command == "minusculas":
                return text.lower()
            elif command == "invertir":
                return text[::-1]
            elif command == "longitud":
                return str(len(text))
            elif command == "capitalizar":
                return text.capitalize()
            elif command == "titulo":
                return text.title()
            elif command == "intercambiar_caso":
                return text.swapcase()
            elif command == "contar_palabras":
                return str(len(text.split()))
            elif command == "recortar":
                return text.strip()
            else:
                return f"Comando no reconocido: {command}"
                
        except Exception as e:
            return f"Error al procesar texto: {str(e)}"

# Cliente RPC
class RpcClient(object):
    """RPC client with reconnection capabilities."""
    
    def __init__(self, host, username, password, rpc_queue, vhost, port=5671, ssl=True):
        self.queue = {}
        self.host = host
        self.username = username
        self.password = password
        self.vhost = vhost
        self.port = port
        self.ssl = ssl
        self.channel = None
        self.connection = None
        self.callback_queue = None
        self.rpc_queue = rpc_queue
        self.consumer_thread = None
        # No conectar en el constructor
    
    def connect(self):
        """Establish a new connection for a single request."""
        try:
            # Crear conexión con soporte SSL/TLS
            self.connection = amqpstorm.Connection(
                self.host, 
                self.username,
                self.password,
                virtual_host=self.vhost,
                port=self.port,
                ssl=self.ssl,
                heartbeat=30
            )
            self.channel = self.connection.channel()
            self.channel.queue.declare(self.rpc_queue)
            result = self.channel.queue.declare(exclusive=True)
            self.callback_queue = result['queue']
            
            return True
        except Exception as e:
            print(f"Error al conectar con RabbitMQ: {str(e)}")
            return False
    
    def send_request(self, payload, timeout=10):
        """Send a request and wait for response using a fresh connection."""
        result = None
        
        try:
            # Crear una nueva conexión para esta solicitud
            if not self.connect():
                raise Exception("No se pudo conectar a RabbitMQ")
            
            # Configurar un identificador de correlación único
            import uuid
            correlation_id = str(uuid.uuid4())
            
            # Variable para almacenar la respuesta
            response = [None]
            
            # Definir el manejador de la respuesta
            def on_response(message):
                if message.correlation_id == correlation_id:
                    response[0] = message.body
                    message.ack()
            
            # Configurar el consumidor para la respuesta
            self.channel.basic.consume(on_response, no_ack=False, queue=self.callback_queue)
            
            # Crear y enviar mensaje
            message = Message.create(
                self.channel,
                payload,
                {
                    'correlation_id': correlation_id,
                    'reply_to': self.callback_queue
                }
            )
            message.publish(routing_key=self.rpc_queue)
            
            # Esperar la respuesta con timeout
            start_time = time.time()
            while response[0] is None:
                # Procesar eventos manualmente
                self.connection.process_data_events()
                
                # Verificar timeout
                if time.time() - start_time > timeout:
                    break
                
                # Pequeña pausa para no saturar CPU
                time.sleep(0.1)
            
            # Almacenar resultado
            result = response[0]
            
        except Exception as e:
            print(f"Error en solicitud RPC: {str(e)}")
            result = None
        
        finally:
            # Siempre cerrar la conexión
            try:
                if self.connection and self.connection.is_open:
                    self.connection.close()
            except:
                pass
            
            self.connection = None
            self.channel = None
        
        return result
    
    def is_connected(self):
        """Verificar si puede establecerse una conexión."""
        try:
            if self.connect():
                # Cerrar la conexión de prueba
                if self.connection and self.connection.is_open:
                    self.connection.close()
                return True
            return False
        except:
            return False

# Lista de operaciones disponibles
TEXT_OPERATIONS = [
    {"id": "mayusculas", "name": "Convertir a MAYÚSCULAS", "icon": "arrow-up-square", "description": "Convierte todo el texto a mayúsculas"},
    {"id": "minusculas", "name": "Convertir a minúsculas", "icon": "arrow-down-square", "description": "Convierte todo el texto a minúsculas"},
    {"id": "invertir", "name": "Invertir texto", "icon": "arrow-left-right", "description": "Invierte el orden de los caracteres del texto"},
    {"id": "longitud", "name": "Longitud del texto", "icon": "rulers", "description": "Cuenta el número de caracteres en el texto"},
    {"id": "capitalizar", "name": "Capitalizar", "icon": "type-bold", "description": "Convierte a mayúscula la primera letra del texto"},
    {"id": "titulo", "name": "Formato título", "icon": "card-heading", "description": "Convierte a mayúscula la primera letra de cada palabra"},
    {"id": "intercambiar_caso", "name": "Intercambiar caso", "icon": "arrow-down-up", "description": "Invierte mayúsculas/minúsculas"},
    {"id": "contar_palabras", "name": "Contar palabras", "icon": "list-ol", "description": "Cuenta el número de palabras en el texto"},
    {"id": "recortar", "name": "Recortar espacios", "icon": "scissors", "description": "Elimina espacios al inicio y final del texto"}
]

# Función para guardar historial
def save_history(operation, input_text, result):
    if 'history' not in session:
        session['history'] = []
    
    history_item = {
        'timestamp': datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        'operation': operation,
        'input_text': input_text,
        'result': result
    }
    
    session['history'] = [history_item] + session['history'][:19]  # Guardar los últimos 20
    session.modified = True

# Iniciar el servidor RPC en un hilo separado
def start_rpc_server():
    """Función para iniciar el servidor RPC en un hilo separado."""
    # Imprimir información de debug
    print(f"Iniciando servidor RPC con:")
    print(f"Host: {RABBIT_HOST}, Puerto: {RABBIT_PORT}, SSL: {RABBIT_SSL}")
    print(f"Usuario: {RABBIT_USER}, vhost: {RABBIT_VHOST}")
    
    # Crear e iniciar el servidor
    server = TextProcessingServer(
        RABBIT_HOST, 
        RABBIT_USER, 
        RABBIT_PASSWORD, 
        RPC_QUEUE,
        RABBIT_VHOST,
        RABBIT_PORT,
        RABBIT_SSL
    )
    
    try:
        server.start()
    except KeyboardInterrupt:
        print("Servidor RPC detenido por el usuario")
    except Exception as e:
        print(f"Error en el servidor RPC: {str(e)}")

# Crear cliente RPC
RPC_CLIENT = RpcClient(
    RABBIT_HOST, 
    RABBIT_USER, 
    RABBIT_PASSWORD, 
    RPC_QUEUE, 
    RABBIT_VHOST,
    RABBIT_PORT,
    RABBIT_SSL
)

# Variable para controlar si el servidor se ha iniciado
server_started = False

# Crear una función para iniciar el servidor RPC
def initialize_rpc_server():
    global server_started
    if not SERVER_STATUS["running"] and not server_started:
        server_started = True
        rpc_thread = threading.Thread(target=start_rpc_server)
        rpc_thread.daemon = True
        rpc_thread.start()
        print("Servidor RPC iniciado")

@app.route('/')
def index():
    """Página principal."""
    # Iniciar servidor RPC si no está en ejecución
    initialize_rpc_server()
    
    connected = RPC_CLIENT.is_connected()
    return render_template('index.html', 
                          operations=TEXT_OPERATIONS, 
                          connected=connected,
                          history=session.get('history', []))

@app.route('/about')
def about():
    """Página acerca de."""
    return render_template('about.html')

@app.route('/process', methods=['POST'])
def process_text():
    """Procesar texto vía RPC."""
    operation = request.form.get('operation')
    text = request.form.get('text')
    
    if not operation or not text:
        flash('Por favor, completa todos los campos', 'danger')
        return redirect(url_for('index'))
    
    # Preparar payload
    payload = f"{operation}:{text}"
    
    # Enviar solicitud RPC y obtener respuesta directamente
    result = RPC_CLIENT.send_request(payload)
    
    if result is None:
        flash('Error al procesar la solicitud', 'danger')
        return redirect(url_for('index'))
    
    # Encontrar el nombre descriptivo de la operación
    operation_name = operation
    for op in TEXT_OPERATIONS:
        if op['id'] == operation:
            operation_name = op['name']
            break
    
    # Guardar en historial
    save_history(operation_name, text, result)
    
    # Si es AJAX, devolver JSON
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'operation': operation_name,
            'result': result
        })
    
    # Si no es AJAX, redireccionar con mensaje
    flash(f'Operación completada: {operation_name}', 'success')
    return redirect(url_for('index'))

@app.route('/clear-history', methods=['POST'])
def clear_history():
    """Borrar historial de operaciones."""
    session.pop('history', None)
    flash('Historial eliminado', 'info')
    return redirect(url_for('index'))

@app.route('/health')
def health_check():
    """Verificar estado de la aplicación y conexión RPC."""
    # Iniciar servidor RPC si no está en ejecución
    initialize_rpc_server()
    
    return jsonify({
        'app': 'ok',
        'rpc_server': SERVER_STATUS,
        'rpc_client_connected': RPC_CLIENT.is_connected()
    })

if __name__ == '__main__':
    # Obtener el puerto del entorno (Render lo proporciona)
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando aplicación web en puerto: {port}")
    
    # Iniciar el servidor RPC para desarrollo local
    initialize_rpc_server()
    
    # Iniciar la aplicación web Flask
    app.run(host='0.0.0.0', port=port)
