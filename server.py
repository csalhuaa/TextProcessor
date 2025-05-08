import os
import threading
from urllib.parse import urlparse
import amqpstorm
from amqpstorm import Message
from flask import Flask, jsonify

# Crear la aplicación Flask
app = Flask(__name__)

# Configuración de CloudAMQP
CLOUDAMQP_URL = os.environ.get('CLOUDAMQP_URL', 'amqps://tnluigbk:x9gWN83qzJ3CIZjiKKAyg327wKNb9eA1@porpoise.rmq.cloudamqp.com/tnluigbk')
url = urlparse(CLOUDAMQP_URL)

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
        """Iniciar el servidor RPC."""
        try:
            # Crear conexión con soporte SSL/TLS
            self.connection = amqpstorm.Connection(
                self.host, 
                self.username,
                self.password,
                virtual_host=self.vhost,
                port=self.port,
                ssl=self.ssl
            )
            # Crear canal
            self.channel = self.connection.channel()
            # Declarar la cola de solicitudes
            self.channel.queue.declare(self.rpc_queue)
            # Establecer QoS
            self.channel.basic.qos(prefetch_count=1)
            # Comenzar a consumir mensajes
            self.channel.basic.consume(self._process_request, self.rpc_queue)
            
            print(f"[x] Servidor de Procesamiento de Texto iniciado. Esperando mensajes en la cola '{self.rpc_queue}'...")
            SERVER_STATUS["running"] = True
            
            # Iniciar el consumo de mensajes
            self.channel.start_consuming()
            
        except Exception as e:
            SERVER_STATUS["running"] = False
            SERVER_STATUS["errors"] += 1
            print(f"Error en el servidor RPC: {str(e)}")
            
    def _process_request(self, message):
        """Procesar solicitudes RPC entrantes."""
        try:
            # Extraer el cuerpo del mensaje (payload)
            payload = message.body
            print(f"[.] Solicitud recibida: {payload}")
            
            # Procesar el texto según el comando
            response = self._process_text(payload)
            
            # Crear un mensaje de respuesta
            response_message = Message.create(
                message.channel,
                response
            )
            
            # Establecer el correlation_id y responder a la cola de callback apropiada
            response_message.correlation_id = message.correlation_id
            response_message.properties['delivery_mode'] = 2  # Hacer el mensaje persistente
            
            # Publicar el mensaje de respuesta
            response_message.publish(routing_key=message.reply_to)
            print(f"[x] Respuesta enviada: {response}")
            
            # Confirmar el mensaje
            message.ack()
            
            # Actualizar estadísticas
            SERVER_STATUS["processed_messages"] += 1
            
        except Exception as e:
            SERVER_STATUS["errors"] += 1
            print(f"Error procesando mensaje: {str(e)}")
            # Intentar confirmar el mensaje para no procesarlo de nuevo
            try:
                message.ack()
            except:
                pass
        
    def _process_text(self, payload):
        """Procesar el texto según el comando proporcionado."""
        # Formato esperado: "comando:texto"
        try:
            # Dividir el payload en comando y texto
            parts = payload.split(':', 1)
            
            if len(parts) < 2:
                return "ERROR: Formato inválido. Se espera 'comando:texto'"
            
            comando = parts[0].lower()
            texto = parts[1]
            
            # Procesar según el comando
            if comando == "mayusculas":
                return texto.upper()
            elif comando == "minusculas":
                return texto.lower()
            elif comando == "invertir":
                return texto[::-1]
            elif comando == "longitud":
                return str(len(texto))
            elif comando == "capitalizar":
                return texto.capitalize()
            elif comando == "titulo":
                return texto.title()
            elif comando == "intercambiar_caso":
                return texto.swapcase()
            elif comando == "contar_palabras":
                return str(len(texto.split()))
            elif comando == "recortar":
                return texto.strip()
            elif comando == "ayuda":
                return "Comandos disponibles: mayusculas, minusculas, invertir, longitud, capitalizar, titulo, intercambiar_caso, contar_palabras, recortar"
            else:
                return f"ERROR: Comando desconocido '{comando}'"
        except Exception as e:
            return f"ERROR: {str(e)}"

# Definir rutas para la aplicación web
@app.route('/')
def index():
    """Endpoint de estado del servidor."""
    return jsonify({
        "service": "TextPro RPC Server",
        "status": "online" if SERVER_STATUS["running"] else "offline",
        "queue": RPC_QUEUE,
        "processed_messages": SERVER_STATUS["processed_messages"],
        "errors": SERVER_STATUS["errors"]
    })

@app.route('/health')
def health():
    """Endpoint de salud para Render."""
    return jsonify({"status": "healthy"})

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
        print("Servidor detenido por el usuario")
    except Exception as e:
        print(f"Error al iniciar el servidor: {str(e)}")
        SERVER_STATUS["running"] = False
        SERVER_STATUS["errors"] += 1

if __name__ == "__main__":
    # Iniciar el servidor RPC en un hilo separado
    rpc_thread = threading.Thread(target=start_rpc_server)
    rpc_thread.daemon = True  # El hilo se cerrará cuando el programa principal termine
    rpc_thread.start()
    
    # Obtener el puerto del entorno de Render
    port = int(os.environ.get('PORT', 10000))
    print(f"Iniciando aplicación web en puerto: {port}")
    
    # Iniciar la aplicación web Flask
    # Importante: usar 0.0.0.0 para escuchar en todas las interfaces
    app.run(host='0.0.0.0', port=port)
