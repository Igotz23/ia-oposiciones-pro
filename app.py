import streamlit as st
import json
import os
import base64
import re
import time
from io import BytesIO
from pypdf import PdfReader
from pdf2image import convert_from_bytes
from groq import Groq, RateLimitError
from dotenv import load_dotenv
from PIL import Image

try:
    from supabase import create_client, Client
    SUPABASE_DISPONIBLE = True
except ImportError:
    SUPABASE_DISPONIBLE = False

load_dotenv()

# --- CLIENTE DE SUPABASE (OPCIONAL: solo si hay credenciales en el .env) ---
supabase_client = None
if SUPABASE_DISPONIBLE and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
    try:
        supabase_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    except Exception:
        supabase_client = None

# Lista de tablas/bases de datos disponibles en Supabase. Añade aquí más según crees nuevas partes.
TABLAS_SUPABASE_DISPONIBLES = [
    "Aplicaciones_teorica_Parte1",
    "Aplicaciones_teorica_Parte2",
    "Sistemas_practicas",
    "Sistemas_teorica"
]

# --- LLAMADA A GROQ CON REINTENTOS AUTOMÁTICOS ANTE RATE LIMIT (429) ---
def groq_completion_con_reintentos(client, max_reintentos=5, **kwargs):
    """Llama a client.chat.completions.create reintentando si Groq devuelve 429 (rate limit)."""
    for intento in range(max_reintentos):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            if intento == max_reintentos - 1:
                raise
            # Intentamos leer el tiempo de espera sugerido por Groq; si no, backoff progresivo
            espera = 2 * (intento + 1)
            try:
                msg = str(e)
                match = re.search(r'try again in ([\d.]+)ms', msg)
                if match:
                    espera = max(float(match.group(1)) / 1000, 0.5)
                else:
                    match_s = re.search(r'try again in ([\d.]+)s', msg)
                    if match_s:
                        espera = float(match_s.group(1))
            except Exception:
                pass
            st.toast(f"⏳ Límite de velocidad alcanzado, reintentando en {espera:.1f}s...")
            time.sleep(espera + 0.3)

# --- CACHÉ EN DISCO: EVITA VOLVER A GASTAR TOKENS EN UN PDF YA PROCESADO ---
import hashlib

CARPETA_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_examenes")
os.makedirs(CARPETA_CACHE, exist_ok=True)

def hash_archivo(bytes_archivo):
    """Genera un identificador único a partir del contenido del archivo (no cambia aunque lo renombres)."""
    return hashlib.sha256(bytes_archivo).hexdigest()

def ruta_cache(hash_id, sufijo):
    return os.path.join(CARPETA_CACHE, f"{hash_id}_{sufijo}.json")

def guardar_en_cache(hash_id, sufijo, datos):
    try:
        with open(ruta_cache(hash_id, sufijo), "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)
    except Exception:
        pass  # la caché es un extra, si falla no debe romper la app

def cargar_de_cache(hash_id, sufijo):
    ruta = ruta_cache(hash_id, sufijo)
    if os.path.exists(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def _normalizar_opciones(opciones):
    """Garantiza que 'opciones' termine siendo una lista de dicts {'letra':..,'texto':..},
    sin importar si Supabase/JSON las devuelve como string, string doblemente
    serializado, o con elementos individuales serializados como string."""
    # Paso 1: si el conjunto entero llegó como string, parsear (una o dos veces si hace falta)
    intentos = 0
    while isinstance(opciones, str) and intentos < 3:
        try:
            opciones = json.loads(opciones)
        except (json.JSONDecodeError, TypeError):
            break
        intentos += 1

    # Paso 2: si el conjunto llegó como dict tipo {"a": "texto a", "b": "texto b", ...},
    # lo convertimos a la lista de dicts {'letra':..,'texto':..} que espera el resto de la app.
    if isinstance(opciones, dict):
        opciones = [{"letra": letra, "texto": texto} for letra, texto in opciones.items()]

    if not isinstance(opciones, list):
        return []

    # Paso 3: normalizar cada elemento individual
    normalizadas = []
    for opt in opciones:
        if isinstance(opt, str):
            try:
                opt = json.loads(opt)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(opt, dict) and "letra" in opt and "texto" in opt:
            normalizadas.append(opt)
    # Aseguramos orden alfabético estable por letra (a, b, c, d...)
    normalizadas.sort(key=lambda o: str(o.get("letra", "")))
    return normalizadas


# --- FUNCIONES AUXILIARES DE SUPABASE ---
def subir_preguntas_a_supabase(nombre_tabla, preguntas, hash_id, nombre_archivo):
    """Sube las preguntas extraídas de un PDF nuevo a la tabla elegida de Supabase.
    Usa upsert por (examen_hash, numero) para no duplicar si se repite el envío."""
    if supabase_client is None:
        return False, "Supabase no está configurado (faltan SUPABASE_URL / SUPABASE_KEY en el .env)."
    try:
        filas = []
        for p in preguntas:
            opciones = _normalizar_opciones(p.get("opciones"))
            filas.append({
                "numero": p.get("numero"),
                "pregunta": p.get("pregunta"),
                "opciones": opciones,
                "examen_hash": hash_id,
                "nombre_archivo": nombre_archivo,
            })
        supabase_client.table(nombre_tabla).upsert(filas, on_conflict="examen_hash,numero").execute()
        return True, f"{len(filas)} preguntas subidas a '{nombre_tabla}'."
    except Exception as e:
        return False, f"No se pudo subir a Supabase: {e}"


def cargar_preguntas_desde_supabase(nombre_tabla):
    """Recupera todas las preguntas guardadas en una tabla de Supabase, ordenadas por número."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        respuesta = supabase_client.table(nombre_tabla).select("numero, pregunta, opciones").order("numero").execute()
        preguntas = [
            {
                "numero": fila["numero"],
                "pregunta": fila["pregunta"],
                "opciones": _normalizar_opciones(fila["opciones"]),
            }
            for fila in respuesta.data
        ]
        return preguntas, None
    except Exception as e:
        return None, f"No se pudo leer de Supabase: {e}"

st.set_page_config(page_title="IA Oposiciones - Escaneados Pro", layout="wide")
st.title("🛡️ Sistema Inteligente de Exámenes (Soporta PDFs Escaneados)")

# --- INICIALIZAR CLIENTE DE GROQ DIRECTO ---
if "GROQ_API_KEY" in os.environ:
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
else:
    st.error("🔑 Falta la variable GROQ_API_KEY en tu archivo .env")
    st.stop()

# --- ESTADO DE LA SESIÓN ---
if "texto_examen" not in st.session_state:
    st.session_state.texto_examen = ""
if "preguntas_examen" not in st.session_state:
    st.session_state.preguntas_examen = None
if "respuestas_usuario" not in st.session_state:
    st.session_state.respuestas_usuario = {}
if "corregido" not in st.session_state:
    st.session_state.corregido = False
if "historial_chat" not in st.session_state:
    st.session_state.historial_chat = []
if "plantilla_respuestas" not in st.session_state:
    st.session_state.plantilla_respuestas = {}
if "texto_respuestas" not in st.session_state:
    st.session_state.texto_respuestas = ""
if "ultima_respuesta_estructuracion" not in st.session_state:
    st.session_state.ultima_respuesta_estructuracion = ""

# --- FUNCIÓN AUXILIAR PARA REDUCIR TAMAÑO Y AHORRAR TOKENS ---
def redimensionar_para_vision(img, ancho_max=1200):
    """Reduce la resolución de la imagen si excede ancho_max, para gastar menos tokens en la API de visión."""
    if img.width > ancho_max:
        ratio = ancho_max / img.width
        nuevo_alto = int(img.height * ratio)
        img = img.resize((ancho_max, nuevo_alto), Image.LANCZOS)
    return img

# --- FUNCIÓN AUXILIAR PARA CONVERTIR IMAGEN A JPEG ---
def convertir_a_jpeg(img):
    """Convierte cualquier imagen a modo RGB para guardar como JPEG."""
    if img.mode in ('RGBA', 'LA', 'P'):
        fondo = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        if img.mode in ('RGBA', 'LA'):
            fondo.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            return fondo
    return img.convert('RGB') if img.mode != 'RGB' else img

# --- BARRA LATERAL: CARGA Y PROCESAMIENTO ---
with st.sidebar:
    st.header("🗄️ Base de datos (Supabase)")
    if supabase_client is None:
        st.caption("⚠️ Supabase no configurado. Añade SUPABASE_URL y SUPABASE_KEY al .env para activarlo.")
    tabla_seleccionada = st.selectbox(
        "Tabla / temario a usar:",
        options=TABLAS_SUPABASE_DISPONIBLES,
        key="tabla_supabase_actual",
        help="Cada tabla guarda las preguntas de un temario o parte distinta del examen."
    )
    if supabase_client is not None and st.button("☁️ Cargar preguntas guardadas de esta tabla"):
        with st.spinner(f"Consultando '{tabla_seleccionada}' en Supabase..."):
            preguntas_supabase, error_supabase = cargar_preguntas_desde_supabase(tabla_seleccionada)
            if error_supabase:
                st.error(error_supabase)
            elif preguntas_supabase:
                st.session_state.preguntas_examen = preguntas_supabase
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.success(f"✅ Cargadas {len(preguntas_supabase)} preguntas desde Supabase.")
            else:
                st.warning("Esa tabla todavía no tiene preguntas guardadas.")

    st.header("📥 Carga del Examen")
    uploaded_file = st.file_uploader("Sube el PDF del examen (Digital o Escaneado)", type="pdf")
    
    st.header("📝 Carga de Respuestas Correctas")
    uploaded_respuestas = st.file_uploader(
        "Sube imagen (PNG/JPG) o PDF con las respuestas correctas", 
        type=["png", "jpg", "jpeg", "pdf"]
    )
    
    if uploaded_respuestas is not None and st.button("🔍 Leer Respuestas Correctas"):
        with st.spinner("Leyendo archivo de respuestas..."):
            try:
                resp_bytes = uploaded_respuestas.read()
                hash_id_resp = hash_archivo(resp_bytes)
                cache_respuestas = cargar_de_cache(hash_id_resp, "respuestas")

                if cache_respuestas is not None:
                    st.session_state.texto_respuestas = cache_respuestas["texto_respuestas"]
                    st.session_state.plantilla_respuestas = cache_respuestas["plantilla"]
                    st.success(
                        f"⚡ Respuestas cargadas desde caché local (0 tokens gastados): "
                        f"{len(cache_respuestas['plantilla'])} preguntas"
                    )
                    st.stop()

                texto_respuestas = ""
                
                # Si es PDF, intentar extraer texto primero
                if uploaded_respuestas.name.lower().endswith(".pdf"):
                    reader = PdfReader(BytesIO(resp_bytes))
                    for page in reader.pages:
                        texto_respuestas += page.extract_text() or ""
                
                # Si no hay texto o es imagen, usar visión
                if not texto_respuestas.strip():
                    st.info("📸 Usando IA de Visión para leer respuestas...")
                    
                    # Convertir a imagen si es PDF, o usar directamente si es imagen
                    if uploaded_respuestas.name.lower().endswith(".pdf"):
                        paginas = convert_from_bytes(resp_bytes, poppler_path=r"C:\poppler\bin")
                        imagenes = paginas
                    else:
                        imagenes = [Image.open(BytesIO(resp_bytes))]
                    
                    for i, img in enumerate(imagenes):
                        # CORRECCIÓN: Convertir a RGB antes de guardar como JPEG
                        img_rgb = convertir_a_jpeg(img)
                        img_rgb = redimensionar_para_vision(img_rgb)
                        buffered = BytesIO()
                        img_rgb.save(buffered, format="JPEG", quality=75)
                        img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                        
                        prompt_resp = (
                            "Extrae ÚNICAMENTE las respuestas correctas de este documento. "
                            "Devuélvelo en formato JSON como un objeto donde la clave es el número de pregunta "
                            "y el valor es la letra de la respuesta correcta (a, b, c o d). "
                            "Ejemplo: {\"1\": \"a\", \"2\": \"c\", \"3\": \"b\"}. "
                            "NO incluyas explicaciones, solo el JSON puro."
                        )
                        
                        response = groq_completion_con_reintentos(client,
                            model="qwen/qwen3.6-27b",
                            messages=[{
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt_resp},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}
                                ]
                            }],
                            temperature=0
                        )
                        texto_respuestas += response.choices[0].message.content + "\n"
                
                st.session_state.texto_respuestas = texto_respuestas
                
                # Extraer JSON de la respuesta
                json_match = re.search(r'\{.*\}', texto_respuestas, re.DOTALL)
                if json_match:
                    try:
                        plantilla = json.loads(json_match.group())
                        st.session_state.plantilla_respuestas = {str(k): str(v).lower().strip() for k, v in plantilla.items()}
                        st.success(f"✅ Respuestas cargadas: {len(st.session_state.plantilla_respuestas)} preguntas")
                        # Guardamos en caché para no volver a gastar tokens con este mismo archivo
                        guardar_en_cache(hash_id_resp, "respuestas", {
                            "texto_respuestas": texto_respuestas,
                            "plantilla": st.session_state.plantilla_respuestas
                        })
                    except json.JSONDecodeError:
                        st.warning("⚠️ No se pudo parsear el JSON. Revisa la pestaña de diagnóstico.")
                else:
                    st.warning("⚠️ No se encontró JSON en la respuesta. Revisa la pestaña de diagnóstico.")
                    
            except Exception as e:
                st.error(f"Error leyendo respuestas: {e}")
    
    # Mostrar respuestas cargadas
    if st.session_state.plantilla_respuestas:
        with st.expander("📋 Ver respuestas correctas cargadas"):
            st.json(st.session_state.plantilla_respuestas)
    
    if uploaded_file is not None and st.button("🚀 Procesar Examen"):
        with st.spinner("Analizando documento..."):
            try:
                # 0. Comprobar si este PDF ya se procesó antes (mismo contenido exacto) para no gastar tokens de nuevo
                file_bytes = uploaded_file.read()
                hash_id = hash_archivo(file_bytes)
                resultado_cacheado = cargar_de_cache(hash_id, "resultado")

                if resultado_cacheado is not None:
                    st.session_state.texto_examen = resultado_cacheado["texto_examen"]
                    st.session_state.preguntas_examen = resultado_cacheado["preguntas"]
                    st.session_state.respuestas_usuario = {}
                    st.session_state.corregido = False
                    st.success(
                        f"⚡ Cargado desde caché local (0 tokens gastados): "
                        f"{len(resultado_cacheado['preguntas'])} preguntas."
                    )
                    st.stop()

                # 1. Intentar leer texto digital primero
                reader = PdfReader(BytesIO(file_bytes))
                texto_completo = ""
                for page in reader.pages:
                    texto_completo += page.extract_text() or ""
                
                # 2. Si no hay texto, ¡es un PDF escaneado! Activamos la IA de Visión y Poppler
                if not texto_completo.strip():
                    st.info("📸 PDF escaneado detectado. Convirtiendo páginas a imágenes con Poppler...")
                    
                    paginas_imagenes = convert_from_bytes(file_bytes, poppler_path=r"C:\poppler\bin")
                    
                    st.write(f"Procesando {len(paginas_imagenes)} páginas con Visión artificial...")
                    
                    texto_completo = ""
                    
                    for i, pagina in enumerate(paginas_imagenes):
                        with st.spinner(f"Leyendo página {i+1}..."):
                            # CORRECCIÓN: Convertir a RGB antes de guardar como JPEG
                            pagina_rgb = convertir_a_jpeg(pagina)
                            pagina_rgb = redimensionar_para_vision(pagina_rgb)
                            buffered = BytesIO()
                            pagina_rgb.save(buffered, format="JPEG", quality=75)
                            img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                            
                            prompt_vision = (
                                "Transcribe de forma LITERAL todo el texto que veas en esta página de examen, "
                                "incluyendo los números de pregunta, enunciados y las opciones de respuesta (a, b, c, d). "
                                "No agregues comentarios propios ni introducciones."
                            )
                            
                            response_vision = groq_completion_con_reintentos(client,
                                model="qwen/qwen3.6-27b",
                                messages=[{
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt_vision},
                                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}
                                    ]
                                }],
                                temperature=0
                            )
                            # CORREGIDO: era response_choices[0], ahora response_vision.choices[0]
                            texto_completo += response_vision.choices[0].message.content + "\n"
                
                st.session_state.texto_examen = texto_completo

                # --- FUNCIÓN AUXILIAR: TROCEAR EL TEXTO RESPETANDO PREGUNTAS COMPLETAS ---
                def dividir_texto_en_bloques(texto, max_caracteres=2500):
                    """Divide el texto en bloques que no superen max_caracteres, cortando SIEMPRE
                    en el inicio de una pregunta nueva (detectada por su numeración), nunca a mitad,
                    para evitar preguntas partidas, duplicadas o desordenadas entre bloques."""
                    lineas = texto.split("\n")

                    # Detectamos qué líneas parecen el inicio de una pregunta numerada
                    patron_inicio_pregunta = re.compile(r'^\s*(?:pregunta\s*)?\d{1,3}[.)\-]\s+', re.IGNORECASE)
                    indices_inicio = [i for i, linea in enumerate(lineas) if patron_inicio_pregunta.match(linea)]

                    if not indices_inicio:
                        # Fallback: si no detectamos numeración, usamos el corte simple por líneas
                        bloques = []
                        actual = []
                        longitud_actual = 0
                        for linea in lineas:
                            longitud_linea = len(linea) + 1
                            if longitud_actual + longitud_linea > max_caracteres and actual:
                                bloques.append("\n".join(actual))
                                actual = []
                                longitud_actual = 0
                            actual.append(linea)
                            longitud_actual += longitud_linea
                        if actual:
                            bloques.append("\n".join(actual))
                        return bloques

                    # Agrupamos preguntas completas (de un inicio de pregunta al siguiente) en bloques
                    # sin superar max_caracteres, cortando SOLO en límites de pregunta.
                    segmentos_preguntas = []
                    for idx, inicio in enumerate(indices_inicio):
                        fin = indices_inicio[idx + 1] if idx + 1 < len(indices_inicio) else len(lineas)
                        segmentos_preguntas.append("\n".join(lineas[inicio:fin]))

                    # Si hay texto antes de la primera pregunta detectada (cabecera/instrucciones), lo descartamos
                    bloques = []
                    actual = []
                    longitud_actual = 0
                    for segmento in segmentos_preguntas:
                        longitud_segmento = len(segmento) + 1
                        if longitud_actual + longitud_segmento > max_caracteres and actual:
                            bloques.append("\n".join(actual))
                            actual = []
                            longitud_actual = 0
                        actual.append(segmento)
                        longitud_actual += longitud_segmento
                    if actual:
                        bloques.append("\n".join(actual))
                    return bloques

                def extraer_lista_preguntas(texto_json):
                    """Intenta parsear el JSON devuelto y sacar la lista de preguntas, con reparación básica."""
                    candidato = texto_json.strip()
                    candidato = re.sub(r'^```(json)?|```$', '', candidato, flags=re.MULTILINE).strip()
                    try:
                        datos = json.loads(candidato)
                    except json.JSONDecodeError:
                        reparado = re.sub(r',\s*([\]}])', r'\1', candidato)
                        datos = json.loads(reparado)

                    if isinstance(datos, dict) and "preguntas" in datos:
                        return datos["preguntas"]
                    if isinstance(datos, list):
                        return datos
                    raise ValueError("El JSON no contiene una lista de 'preguntas' reconocible.")

                # 3. Generar cuestionario JSON estructurado, TROCEANDO el texto para no pasarnos del límite de tokens
                prompt_sistema = (
                    "Eres un extractor de datos de exámenes experto. Tu única tarea es identificar todas las preguntas "
                    "del FRAGMENTO de texto proporcionado junto con sus opciones de respuesta (a, b, c, d). "
                    "El fragmento puede empezar o acabar a mitad de una pregunta; en ese caso, ignora la pregunta incompleta.\n\n"
                    "IMPORTANTE sobre el campo \"numero\": usa SIEMPRE el número que aparece impreso literalmente delante de "
                    "cada pregunta en el texto (por ejemplo, si el texto pone \"30. Al atributo...\", el numero debe ser 30). "
                    "NUNCA renumeres empezando en 1 ni inventes una numeración correlativa propia: el examen puede empezar "
                    "en cualquier número (por ejemplo, un fragmento puede empezar directamente en la pregunta 30).\n\n"
                    "Debes responder EXCLUSIVAMENTE con un objeto JSON válido con esta forma exacta:\n"
                    '{"preguntas": [{"numero": 30, "pregunta": "Texto", "opciones": [{"letra": "a", "texto": "Opt A"}, {"letra": "b", "texto": "Opt B"}]}]}\n\n'
                    "Si el fragmento no contiene ninguna pregunta completa, responde: {\"preguntas\": []}\n"
                    "No incluyas introducciones, comentarios, saludos ni formato markdown."
                )

                bloques = dividir_texto_en_bloques(texto_completo, max_caracteres=2500)
                preguntas_parseadas = []
                respuestas_raw_todas = []
                hubo_error_bloque = False

                barra_progreso = st.progress(0, text="Estructurando cuestionario interactivo...")
                for idx, bloque in enumerate(bloques):
                    barra_progreso.progress(
                        (idx + 1) / len(bloques),
                        text=f"Estructurando bloque {idx + 1}/{len(bloques)}..."
                    )
                    try:
                        completion = groq_completion_con_reintentos(client,
                            model="openai/gpt-oss-120b",
                            messages=[
                                {"role": "system", "content": prompt_sistema},
                                {"role": "user", "content": f"Fragmento del examen:\n\n{bloque}"}
                            ],
                            temperature=0,
                            max_tokens=2000,
                            response_format={"type": "json_object"}
                        )
                        respuesta_raw = completion.choices[0].message.content.strip()
                        respuestas_raw_todas.append(f"--- Bloque {idx + 1} ---\n{respuesta_raw}")

                        preguntas_bloque = extraer_lista_preguntas(respuesta_raw)
                        preguntas_parseadas.extend(preguntas_bloque)

                        # Pequeña pausa para no chocar con el límite de tokens/minuto entre bloques
                        time.sleep(1.2)
                    except (json.JSONDecodeError, ValueError) as e_json:
                        hubo_error_bloque = True
                        respuestas_raw_todas.append(f"--- Bloque {idx + 1} (ERROR: {e_json}) ---")
                        continue

                barra_progreso.empty()
                st.session_state.ultima_respuesta_estructuracion = "\n\n".join(respuestas_raw_todas)

                # Normalizamos "numero" a entero cuando sea posible, para poder ordenar y deduplicar bien
                def numero_a_entero(valor):
                    try:
                        return int(re.search(r'\d+', str(valor)).group())
                    except (AttributeError, ValueError, TypeError):
                        return None

                for p in preguntas_parseadas:
                    p["numero"] = numero_a_entero(p.get("numero"))

                # Deduplicar: si dos preguntas comparten el mismo número detectado, nos quedamos con la primera.
                # Si una pregunta no trae número (raro), deduplicamos por texto en su lugar.
                def normalizar_texto_pregunta(texto):
                    return re.sub(r'\s+', ' ', str(texto)).strip().lower()

                vistos_numero = set()
                vistos_texto = set()
                preguntas_sin_duplicados = []
                for p in preguntas_parseadas:
                    num = p.get("numero")
                    if num is not None:
                        if num in vistos_numero:
                            continue
                        vistos_numero.add(num)
                        preguntas_sin_duplicados.append(p)
                    else:
                        clave = normalizar_texto_pregunta(p.get("pregunta", ""))
                        if clave and clave not in vistos_texto:
                            vistos_texto.add(clave)
                            preguntas_sin_duplicados.append(p)
                preguntas_parseadas = preguntas_sin_duplicados

                # Ordenamos por el número REAL de pregunta (tal y como aparece en el examen, sin reinventarlo).
                # Así, si el PDF empieza en la pregunta 30, se queda en la 30 y no se renumera a partir de 1.
                preguntas_parseadas.sort(key=lambda p: (p.get("numero") is None, p.get("numero")))

                if hubo_error_bloque:
                    st.warning(
                        "⚠️ Algún bloque de texto no se pudo estructurar correctamente y se omitió. "
                        "Revisa la pestaña 🔧 Diagnóstico para ver el detalle."
                    )

                if preguntas_parseadas and len(preguntas_parseadas) > 0:
                    st.session_state.preguntas_examen = preguntas_parseadas
                    st.session_state.respuestas_usuario = {}
                    st.session_state.corregido = False
                    primer_num = preguntas_parseadas[0].get("numero")
                    ultimo_num = preguntas_parseadas[-1].get("numero")
                    st.success(
                        f"✅ ¡Éxito! Detectadas {len(preguntas_parseadas)} preguntas "
                        f"(de la nº{primer_num} a la nº{ultimo_num})."
                    )
                    # Guardamos el resultado en caché: la próxima vez que subas este mismo PDF
                    # se cargará al instante sin gastar tokens de la API.
                    guardar_en_cache(hash_id, "resultado", {
                        "texto_examen": texto_completo,
                        "preguntas": preguntas_parseadas
                    })

                    # Subimos las preguntas nuevas a Supabase, a la tabla elegida en el sidebar
                    if supabase_client is not None:
                        with st.spinner(f"☁️ Subiendo preguntas a Supabase ('{tabla_seleccionada}')..."):
                            ok_supabase, mensaje_supabase = subir_preguntas_a_supabase(
                                tabla_seleccionada, preguntas_parseadas, hash_id, uploaded_file.name
                            )
                            if ok_supabase:
                                st.success(f"☁️ {mensaje_supabase}")
                            else:
                                st.warning(f"⚠️ {mensaje_supabase}")
                else:
                    st.warning("⚠️ La IA no pudo estructurar las preguntas. Revisa la pestaña de diagnóstico.")
                    st.session_state.preguntas_examen = None

            except Exception as e:
                st.error(f"Error al procesar: {e}")

# --- PANTALLA PRINCIPAL: DOS PESTAÑAS ---
tab1, tab2, tab3 = st.tabs(["📝 Cuestionario Interactivo", "💬 Chat con el Examen", "🔧 Diagnóstico"])

with tab1:
    if st.session_state.preguntas_examen is None:
        st.info("👋 Sube tu examen en PDF en la barra lateral izquierda y pulsa 'Procesar Examen'.")
    else:
        st.subheader("✍️ Responde al examen de la oposición")
        
        for item in st.session_state.preguntas_examen:
            num = str(item["numero"])
            st.markdown("---")
            st.markdown(f"**Pregunta {num}:** {item['pregunta']}")
            
            opciones_item = _normalizar_opciones(item.get("opciones"))
            if not opciones_item:
                st.warning(f"⚠️ La pregunta {num} no tiene opciones válidas y se omite.")
                continue
            opciones = [f"{opt['letra'].upper()}) {opt['texto']}" for opt in opciones_item]
            
            index_previo = None
            if num in st.session_state.respuestas_usuario:
                letra_g = st.session_state.respuestas_usuario[num]
                for i, opt in enumerate(opciones_item):
                    if opt["letra"] == letra_g:
                        index_previo = i
            
            seleccion = st.radio(
                f"p_{num}", options=opciones, index=index_previo, 
                key=f"radio_{num}", label_visibility="collapsed", disabled=st.session_state.corregido
            )
            
            if seleccion and not st.session_state.corregido:
                st.session_state.respuestas_usuario[num] = seleccion[0].lower()
            
            # ✅ CORRECCIÓN MEJORADA: Mostrar resultado para TODAS las preguntas
            if st.session_state.corregido:
                marcada = st.session_state.respuestas_usuario.get(num)
                correcta = st.session_state.plantilla_respuestas.get(num)
                
                if correcta:
                    if marcada == correcta:
                        st.success(f"🟢 ¡Correcto! La respuesta es la {correcta.upper()}")
                    elif marcada is None:
                        st.warning(f"🟡 Pregunta no contestada. La correcta era la {correcta.upper()}")
                    else:
                        st.error(f"🔴 Fallaste. Marcaste la {marcada.upper()}. La correcta era la {correcta.upper()}")
                else:
                    # Si no hay respuesta en la plantilla, mostrar aviso
                    if marcada:
                        st.info(f"⚪ Pregunta {num}: Marcaste la {marcada.upper()}. (No hay respuesta correcta cargada para comparar)")
                    else:
                        st.info(f"⚪ Pregunta {num}: Sin contestar. (No hay respuesta correcta cargada para comparar)")

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("📊 Corregir Examen", type="primary", disabled=st.session_state.corregido):
                st.session_state.corregido = True
                st.rerun()
        with c2:
            if st.button("🔄 Reiniciar Respuestas"):
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.rerun()

        # 📊 RESUMEN DE NOTAS (MEJORADO)
        if st.session_state.corregido and st.session_state.preguntas_examen:
            st.markdown("---")
            st.subheader("📊 Resultados del Examen")
            
            total_preguntas = len(st.session_state.preguntas_examen)
            
            # ✅ SOLO las preguntas que están en el examen cargado
            numeros_examen = {str(p["numero"]) for p in st.session_state.preguntas_examen}
            plantilla_filtrada = {
                n: r for n, r in st.session_state.plantilla_respuestas.items()
                if n in numeros_examen
            }
            preguntas_con_respuesta = len(plantilla_filtrada)
            
            # Contar aciertos solo sobre las preguntas del examen actual
            aciertos = sum(
                1 for n in plantilla_filtrada.keys() 
                if st.session_state.respuestas_usuario.get(n) == plantilla_filtrada[n]
            )
            
            fallos = sum(
                1 for n in plantilla_filtrada.keys() 
                if st.session_state.respuestas_usuario.get(n) is not None 
                and st.session_state.respuestas_usuario.get(n) != plantilla_filtrada[n]
            )
            
            blancos = sum(
                1 for n in plantilla_filtrada.keys() 
                if st.session_state.respuestas_usuario.get(n) is None
            )
            
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("✅ Aciertos", aciertos)
            col2.metric("❌ Fallos", fallos)
            col3.metric("⚪ En blanco", blancos)
            
            if preguntas_con_respuesta > 0:
                nota = (aciertos / preguntas_con_respuesta) * 10
                col4.metric("💯 Nota", f"{nota:.2f}/10")
            else:
                col4.metric("💯 Nota", "N/A", help="Sube un archivo con las respuestas correctas")
            
            # Barra de progreso visual
            if preguntas_con_respuesta > 0:
                progreso = aciertos / preguntas_con_respuesta
                st.progress(progreso, text=f"Progreso: {aciertos}/{preguntas_con_respuesta} correctas")
            
            # Aviso si faltan respuestas
            sin_plantilla = [n for n in numeros_examen if n not in plantilla_filtrada]
            if sin_plantilla:
                st.info(f"ℹ️ Preguntas sin respuesta correcta cargada: {', '.join(sorted(sin_plantilla, key=int))}")

with tab2:
    if not st.session_state.texto_examen:
        st.info("👋 Sube un examen primero para poder hacerle preguntas libres.")
    else:
        st.subheader("💬 Chat con el Examen")
        
        for chat in st.session_state.historial_chat:
            with st.chat_message(chat["role"]):
                st.markdown(chat["content"])
        
        if pregunta_usuario := st.chat_input("Ej: ¿Qué dice la pregunta 1?"):
            with st.chat_message("user"):
                st.markdown(pregunta_usuario)
            st.session_state.historial_chat.append({"role": "user", "content": pregunta_usuario})

            # Recortamos el examen y el historial para no superar el límite de tokens/minuto del tier gratis
            MAX_CARACTERES_CONTEXTO = 4000
            examen_para_contexto = st.session_state.texto_examen[:MAX_CARACTERES_CONTEXTO]
            if len(st.session_state.texto_examen) > MAX_CARACTERES_CONTEXTO:
                examen_para_contexto += "\n[...texto recortado por límite de tokens...]"

            prompt_chat = [
                {"role": "system", "content": f"Eres un tutor experto en oposiciones. Responde basándote estrictamente en este examen transcrito:\n\n{examen_para_contexto}"}
            ]
            # Solo los últimos mensajes del historial, para no acumular contexto indefinidamente
            for chat in st.session_state.historial_chat[-6:]:
                prompt_chat.append(chat)
                
            with st.chat_message("assistant"):
                with st.spinner("Pensando..."):
                    completion_chat = groq_completion_con_reintentos(client,
                        model="openai/gpt-oss-120b",
                        messages=prompt_chat,
                        temperature=0.3,
                        max_tokens=1500
                    )
                    respuesta_chat = completion_chat.choices[0].message.content
                    st.markdown(respuesta_chat)
            
            st.session_state.historial_chat.append({"role": "assistant", "content": respuesta_chat})

with tab3:
    st.subheader("🔧 Diagnóstico y Depuración")
    
    with st.expander("📄 Texto del examen transcrito"):
        st.text_area("Contenido:", value=st.session_state.texto_examen, height=300, key="diag_texto_examen")
    
    with st.expander("📄 Texto de respuestas correctas (raw)"):
        st.text_area("Contenido:", value=st.session_state.texto_respuestas, height=200, key="diag_texto_respuestas")
    
    with st.expander("🧩 Última respuesta JSON del modelo (estructuración de preguntas)"):
        st.text_area("Contenido:", value=st.session_state.ultima_respuesta_estructuracion, height=200, key="diag_ultima_respuesta_json")

    with st.expander("📋 Plantilla de respuestas parseada"):
        st.json(st.session_state.plantilla_respuestas)
    
    with st.expander("👤 Respuestas del usuario"):
        st.json(st.session_state.respuestas_usuario)
    
    if st.button("🗑️ Limpiar todo"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

    st.caption(f"📁 Caché local de PDFs ya procesados: `{CARPETA_CACHE}`")
    if st.button("🧹 Borrar caché de PDFs (forzar reprocesar todo desde cero)"):
        borrados = 0
        for archivo in os.listdir(CARPETA_CACHE):
            try:
                os.remove(os.path.join(CARPETA_CACHE, archivo))
                borrados += 1
            except Exception:
                pass
        st.success(f"Caché borrada ({borrados} archivos eliminados).")