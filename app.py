import streamlit as st
import json
import os
import base64
import re
import time
import random
from datetime import datetime, timezone
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
    "Sistemas_practica",
    "Sistemas_teorica",
    "Bateria_aplicaciones",
    "subalterna"
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


def parsear_seleccion_preguntas(texto_seleccion, numeros_disponibles):
    """Parsea un texto tipo '200-300 450 460' en un conjunto de números de pregunta.
    Se puede seleccionar por rango (con guión) o individualmente (separado por espacios).
    Si el texto está vacío, se seleccionan todas las preguntas disponibles."""
    texto = (texto_seleccion or "").strip()
    disponibles = set(numeros_disponibles)

    if not texto:
        return disponibles

    seleccionados = set()
    for token in texto.split():
        token = token.strip()
        if "-" in token:
            partes = token.split("-")
            if len(partes) == 2 and partes[0].strip().lstrip("-").isdigit() and partes[1].strip().isdigit():
                inicio, fin = int(partes[0]), int(partes[1])
                if inicio > fin:
                    inicio, fin = fin, inicio
                seleccionados.update(range(inicio, fin + 1))
        elif token.isdigit():
            seleccionados.add(int(token))

    return seleccionados & disponibles


def cargar_preguntas_desde_supabase(nombre_tabla, seleccion_texto="", orden="Orden numérico"):
    """Recupera las preguntas guardadas en una tabla de Supabase, incluyendo la respuesta
    correcta (columna 'respuesta') para poder corregir sin subir una plantilla aparte.
    Permite filtrar por número de pregunta (rangos o individuales) y elegir el orden
    de presentación: numérico, aleatorio, o aleatorio con las opciones también desordenadas."""
    if supabase_client is None:
        return None, None, "Supabase no está configurado."
    try:
        respuesta = supabase_client.table(nombre_tabla).select("numero, pregunta, opciones, respuesta").order("numero").execute()
        filas = respuesta.data
        if not filas:
            return [], {}, None

        numeros_disponibles = [fila["numero"] for fila in filas if fila.get("numero") is not None]
        numeros_seleccionados = parsear_seleccion_preguntas(seleccion_texto, numeros_disponibles)
        filas_filtradas = [fila for fila in filas if fila.get("numero") in numeros_seleccionados]

        preguntas = []
        plantilla_respuestas = {}
        for fila in filas_filtradas:
            opciones = fila.get("opciones")
            # Compatibilidad: si las opciones vinieran como objeto {"a": "..."} en vez de lista, las normalizamos
            if isinstance(opciones, dict):
                opciones = [{"letra": letra, "texto": texto_opt} for letra, texto_opt in opciones.items()]

            # ✅ "clave" identifica de forma única la pregunta combinando tabla de origen + número,
            # para poder mezclar preguntas de varias tablas (ej. en el Test de errores) sin que
            # choquen dos preguntas con el mismo número pero de tablas distintas.
            clave = f"{nombre_tabla}__{fila['numero']}"
            preguntas.append({
                "numero": fila["numero"],
                "pregunta": fila["pregunta"],
                "opciones": opciones,
                "tabla_origen": nombre_tabla,
                "clave": clave,
            })

            if fila.get("respuesta"):
                plantilla_respuestas[clave] = str(fila["respuesta"]).lower().strip()

        if orden == "Orden numérico":
            preguntas.sort(key=lambda p: p["numero"])
        elif orden == "Orden aleatorio":
            random.shuffle(preguntas)
        elif orden == "Orden aleatorio y respuestas desordenadas":
            random.shuffle(preguntas)
            for p in preguntas:
                if isinstance(p["opciones"], list):
                    random.shuffle(p["opciones"])

        return preguntas, plantilla_respuestas, None
    except Exception as e:
        return None, None, f"No se pudo leer de Supabase: {e}"


# --- BANCO DE ERRORES: REGISTRAR, CARGAR Y LIMPIAR EN SUPABASE ---
def registrar_errores_en_supabase(tabla_origen, numeros_fallados):
    """Sube (o actualiza) en 'errores_usuario' las preguntas falladas/en blanco de un test.
    Si la pregunta ya estaba en el banco de errores, solo incrementa el contador de veces
    fallada en vez de duplicarla (gracias al unique(tabla_origen, numero))."""
    if supabase_client is None or not numeros_fallados:
        return 0, None
    try:
        numeros_fallados = list(numeros_fallados)
        existentes = supabase_client.table("errores_usuario") \
            .select("numero, veces_fallada") \
            .eq("tabla_origen", tabla_origen) \
            .in_("numero", numeros_fallados) \
            .execute().data
        existentes_map = {e["numero"]: e.get("veces_fallada") or 1 for e in existentes}

        nuevos = [n for n in numeros_fallados if n not in existentes_map]
        actualizados = [n for n in numeros_fallados if n in existentes_map]
        ahora = datetime.now(timezone.utc).isoformat()

        if nuevos:
            filas_nuevas = [{"tabla_origen": tabla_origen, "numero": n} for n in nuevos]
            supabase_client.table("errores_usuario").insert(filas_nuevas).execute()

        for n in actualizados:
            supabase_client.table("errores_usuario").update({
                "veces_fallada": existentes_map[n] + 1,
                "ultima_vez_fallada": ahora,
            }).eq("tabla_origen", tabla_origen).eq("numero", n).execute()

        return len(numeros_fallados), None
    except Exception as e:
        return 0, str(e)


def contar_errores_pendientes():
    """Cuenta cuántas preguntas hay actualmente en el banco de errores."""
    if supabase_client is None:
        return None
    try:
        resultado = supabase_client.table("errores_usuario").select("id", count="exact").execute()
        return resultado.count or 0
    except Exception:
        return None


def cargar_preguntas_de_errores():
    """Recupera todas las preguntas guardadas en 'errores_usuario', agrupadas por tabla de
    origen, y trae el enunciado/opciones/respuesta ACTUALES desde cada tabla original
    (no se guarda una copia del contenido, solo la referencia tabla+número)."""
    if supabase_client is None:
        return None, None, "Supabase no está configurado."
    try:
        filas_error = supabase_client.table("errores_usuario").select("tabla_origen, numero").execute().data
        if not filas_error:
            return [], {}, None

        por_tabla = {}
        for fila in filas_error:
            por_tabla.setdefault(fila["tabla_origen"], []).append(fila["numero"])

        preguntas = []
        plantilla_respuestas = {}
        for tabla_origen, numeros in por_tabla.items():
            respuesta = supabase_client.table(tabla_origen) \
                .select("numero, pregunta, opciones, respuesta") \
                .in_("numero", numeros).execute()
            for fila in respuesta.data:
                opciones = fila.get("opciones")
                if isinstance(opciones, dict):
                    opciones = [{"letra": letra, "texto": texto_opt} for letra, texto_opt in opciones.items()]

                clave = f"{tabla_origen}__{fila['numero']}"
                preguntas.append({
                    "numero": fila["numero"],
                    "pregunta": fila["pregunta"],
                    "opciones": opciones,
                    "tabla_origen": tabla_origen,
                    "clave": clave,
                })
                if fila.get("respuesta"):
                    plantilla_respuestas[clave] = str(fila["respuesta"]).lower().strip()

        preguntas.sort(key=lambda p: (p["tabla_origen"], p["numero"]))
        return preguntas, plantilla_respuestas, None
    except Exception as e:
        return None, None, f"No se pudo leer el Test de errores desde Supabase: {e}"


def borrar_errores_acertados(referencias):
    """Borra de 'errores_usuario' las preguntas indicadas (lista de tuplas tabla_origen, numero).
    Esto NUNCA toca la tabla original de preguntas, solo el banco de errores."""
    if supabase_client is None or not referencias:
        return None
    try:
        for tabla_origen, numero in referencias:
            supabase_client.table("errores_usuario").delete() \
                .eq("tabla_origen", tabla_origen).eq("numero", numero).execute()
        return None
    except Exception as e:
        return str(e)


# --- TEST MIXTO: N PREGUNTAS AL AZAR, PROPORCIONAL AL TAMAÑO DE CADA TABLA ---
def cargar_test_mixto(n_preguntas):
    """Genera un examen mezclando N preguntas al azar de TODAS las tablas de
    TABLAS_SUPABASE_DISPONIBLES. El azar es 'puro' sobre el conjunto combinado de todas
    las preguntas existentes, así que las tablas más grandes aportan proporcionalmente
    más preguntas (igual que un examen real mezclado por temarios de distinto peso)."""
    if supabase_client is None:
        return None, None, 0, "Supabase no está configurado."
    try:
        # 1. Consulta ligera: solo pedimos "numero" de cada tabla para construir el pool,
        # sin traernos el contenido completo todavía (más rápido y barato).
        pool = []
        for tabla in TABLAS_SUPABASE_DISPONIBLES:
            filas = supabase_client.table(tabla).select("numero").execute().data
            for fila in filas:
                if fila.get("numero") is not None:
                    pool.append((tabla, fila["numero"]))

        if not pool:
            return [], {}, 0, None

        n_final = min(n_preguntas, len(pool))
        seleccionados = random.sample(pool, n_final)

        # 2. Agrupamos lo seleccionado por tabla para traer el contenido completo
        # con el mínimo de consultas posible (una por tabla implicada, no una por pregunta).
        por_tabla = {}
        for tabla, numero in seleccionados:
            por_tabla.setdefault(tabla, []).append(numero)

        preguntas = []
        plantilla_respuestas = {}
        for tabla_origen, numeros in por_tabla.items():
            respuesta = supabase_client.table(tabla_origen) \
                .select("numero, pregunta, opciones, respuesta") \
                .in_("numero", numeros).execute()
            for fila in respuesta.data:
                opciones = fila.get("opciones")
                if isinstance(opciones, dict):
                    opciones = [{"letra": letra, "texto": texto_opt} for letra, texto_opt in opciones.items()]

                clave = f"{tabla_origen}__{fila['numero']}"
                preguntas.append({
                    "numero": fila["numero"],
                    "pregunta": fila["pregunta"],
                    "opciones": opciones,
                    "tabla_origen": tabla_origen,
                    "clave": clave,
                })
                if fila.get("respuesta"):
                    plantilla_respuestas[clave] = str(fila["respuesta"]).lower().strip()

        # 3. Mezclamos el orden final para que no salgan agrupadas por tabla
        random.shuffle(preguntas)

        return preguntas, plantilla_respuestas, n_final, None
    except Exception as e:
        return None, None, 0, f"No se pudo generar el Test Mixto desde Supabase: {e}"

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
if "modo_test_errores" not in st.session_state:
    st.session_state.modo_test_errores = False
if "modo_test_mixto" not in st.session_state:
    st.session_state.modo_test_mixto = False

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

    st.markdown("**Selecciona las preguntas:**")
    st.caption(
        "Se puede seleccionar temas y preguntas mediante rango (con un guión) o individualmente "
        "por espacios como se muestra en el ejemplo. Si se dejan en blanco se seleccionan todas las preguntas."
    )
    seleccion_preguntas_texto = st.text_input(
        "Preguntas:",
        placeholder="Ejemplo: 200-300 450 460",
        key="seleccion_preguntas_texto"
    )

    st.markdown("**Selecciona el orden:**")
    orden_preguntas = st.radio(
        "orden_preguntas",
        options=["Orden numérico", "Orden aleatorio", "Orden aleatorio y respuestas desordenadas"],
        key="orden_preguntas_radio",
        label_visibility="collapsed"
    )

    if supabase_client is not None and st.button("☁️ Cargar preguntas guardadas de esta tabla"):
        with st.spinner(f"Consultando '{tabla_seleccionada}' en Supabase..."):
            preguntas_supabase, plantilla_supabase, error_supabase = cargar_preguntas_desde_supabase(
                tabla_seleccionada, seleccion_preguntas_texto, orden_preguntas
            )
            if error_supabase:
                st.error(error_supabase)
            elif preguntas_supabase:
                st.session_state.preguntas_examen = preguntas_supabase
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = False
                # ✅ Comparamos directamente contra la respuesta guardada en la BD, sin necesidad
                # de subir una plantilla de respuestas aparte.
                st.session_state.plantilla_respuestas = plantilla_supabase
                mensaje = f"✅ Cargadas {len(preguntas_supabase)} preguntas desde Supabase"
                if plantilla_supabase:
                    mensaje += f" (con {len(plantilla_supabase)} respuestas correctas listas para corregir)."
                else:
                    mensaje += " (esta tabla no tiene respuestas correctas guardadas)."
                st.success(mensaje)
            else:
                st.warning("No hay preguntas que coincidan con esa selección en esta tabla.")

    st.header("🔁 Test de errores")
    num_errores_pendientes = contar_errores_pendientes()
    if num_errores_pendientes is not None:
        if num_errores_pendientes > 0:
            st.caption(f"📌 Tienes **{num_errores_pendientes}** pregunta(s) pendientes en tu banco de errores.")
        else:
            st.caption("📌 Tu banco de errores está vacío. ¡Bien hecho!")
    if supabase_client is not None and st.button("🔁 Cargar Test de errores"):
        with st.spinner("Consultando tu banco de errores en Supabase..."):
            preguntas_error, plantilla_error, error_error = cargar_preguntas_de_errores()
            if error_error:
                st.error(error_error)
            elif preguntas_error:
                st.session_state.preguntas_examen = preguntas_error
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.session_state.plantilla_respuestas = plantilla_error
                st.session_state.modo_test_errores = True
                st.session_state.modo_test_mixto = False
                st.success(f"✅ Cargado Test de errores con {len(preguntas_error)} pregunta(s) pendientes.")
            else:
                st.info("🎉 ¡No tienes errores guardados! Tu banco de errores está vacío.")

    st.header("🧪 Test Mixto")
    st.caption(
        "Genera un examen con preguntas al azar mezclando TODAS las tablas de opos. "
        "El azar es proporcional al tamaño de cada tabla (las tablas más grandes aportan más preguntas)."
    )
    n_preguntas_mixto = st.number_input(
        "¿Cuántas preguntas quieres?",
        min_value=1, max_value=500, value=30, step=1,
        key="n_preguntas_test_mixto"
    )
    if supabase_client is not None and st.button("🎲 Generar Test Mixto"):
        with st.spinner("Sorteando preguntas de todas las tablas..."):
            preguntas_mixto, plantilla_mixto, n_generadas, error_mixto = cargar_test_mixto(n_preguntas_mixto)
            if error_mixto:
                st.error(error_mixto)
            elif preguntas_mixto:
                st.session_state.preguntas_examen = preguntas_mixto
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.session_state.plantilla_respuestas = plantilla_mixto
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = True
                mensaje = f"✅ Test Mixto generado con {n_generadas} pregunta(s)"
                if n_generadas < n_preguntas_mixto:
                    mensaje += f" (pediste {n_preguntas_mixto}, pero solo hay {n_generadas} preguntas disponibles en total)."
                else:
                    mensaje += "."
                st.success(mensaje)
            else:
                st.warning("No se encontraron preguntas en ninguna de las tablas configuradas.")

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
                file_bytes = uploaded_file.read()

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

                # PDF suelto: no viene de ninguna tabla de Supabase, así que no puede
                # añadirse al banco de errores (no hay tabla+numero de referencia).
                for p in preguntas_parseadas:
                    p["tabla_origen"] = None
                    p["clave"] = str(p.get("numero"))

                if preguntas_parseadas and len(preguntas_parseadas) > 0:
                    st.session_state.preguntas_examen = preguntas_parseadas
                    st.session_state.respuestas_usuario = {}
                    st.session_state.corregido = False
                    st.session_state.modo_test_errores = False
                    st.session_state.modo_test_mixto = False
                    primer_num = preguntas_parseadas[0].get("numero")
                    ultimo_num = preguntas_parseadas[-1].get("numero")
                    st.success(
                        f"✅ ¡Éxito! Detectadas {len(preguntas_parseadas)} preguntas "
                        f"(de la nº{primer_num} a la nº{ultimo_num})."
                    )
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
            # ✅ "clave" identifica la pregunta de forma única (tabla_origen + numero cuando
            # viene de Supabase, o solo el numero si viene de un PDF suelto).
            clave = item.get("clave") or str(item["numero"])
            st.markdown("---")
            etiqueta_origen = f" _(de {item['tabla_origen']})_" if (st.session_state.modo_test_errores or st.session_state.modo_test_mixto) and item.get("tabla_origen") else ""
            st.markdown(f"**Pregunta {item['numero']}{etiqueta_origen}:** {item['pregunta']}")
            
            opciones = [f"{opt['letra'].upper()}) {opt['texto']}" for opt in item["opciones"]]
            
            index_previo = None
            if clave in st.session_state.respuestas_usuario:
                letra_g = st.session_state.respuestas_usuario[clave]
                for i, opt in enumerate(item["opciones"]):
                    if opt["letra"] == letra_g:
                        index_previo = i
            
            seleccion = st.radio(
                f"p_{clave}", options=opciones, index=index_previo, 
                key=f"radio_{clave}", label_visibility="collapsed", disabled=st.session_state.corregido
            )
            
            if seleccion and not st.session_state.corregido:
                st.session_state.respuestas_usuario[clave] = seleccion[0].lower()
            
            # ✅ CORRECCIÓN MEJORADA: Mostrar resultado para TODAS las preguntas
            if st.session_state.corregido:
                marcada = st.session_state.respuestas_usuario.get(clave)
                correcta = st.session_state.plantilla_respuestas.get(clave)
                
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
                        st.info(f"⚪ Pregunta {item['numero']}: Marcaste la {marcada.upper()}. (No hay respuesta correcta cargada para comparar)")
                    else:
                        st.info(f"⚪ Pregunta {item['numero']}: Sin contestar. (No hay respuesta correcta cargada para comparar)")

        st.markdown("---")

        # ¿El examen actual viene de alguna tabla de Supabase? (si es un PDF suelto, no hay
        # tabla_origen y por tanto no se puede añadir al banco de errores)
        viene_de_supabase = any(item.get("tabla_origen") for item in st.session_state.preguntas_examen)

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("📊 Corregir Examen", type="primary", disabled=st.session_state.corregido):
                st.session_state.corregido = True

                # Si estamos haciendo el Test de errores, las preguntas ACERTADAS se borran
                # automáticamente del banco de errores (las falladas se quedan para repasar).
                if st.session_state.modo_test_errores:
                    aciertos_para_borrar = []
                    for item in st.session_state.preguntas_examen:
                        clave = item["clave"]
                        correcta = st.session_state.plantilla_respuestas.get(clave)
                        marcada = st.session_state.respuestas_usuario.get(clave)
                        if correcta and marcada == correcta:
                            aciertos_para_borrar.append((item["tabla_origen"], item["numero"]))
                    if aciertos_para_borrar:
                        error_borrado = borrar_errores_acertados(aciertos_para_borrar)
                        if error_borrado:
                            st.toast(f"⚠️ No se pudieron borrar algunos errores: {error_borrado}")
                        else:
                            st.toast(f"🗑️ {len(aciertos_para_borrar)} pregunta(s) acertada(s) borradas de tu banco de errores.")

                st.rerun()
        with c2:
            if st.button("🔄 Reiniciar Respuestas"):
                st.session_state.respuestas_usuario = {}
                st.session_state.corregido = False
                st.rerun()
        with c3:
            # 🔒 Oculto hasta que se corrija el examen. No aparece en modo Test de errores
            # (ese banco ya se autogestiona al corregir) ni en exámenes de PDF suelto.
            if st.session_state.corregido and viene_de_supabase and not st.session_state.modo_test_errores:
                if st.button("➕ Añadir errores a Test de errores"):
                    fallos_por_tabla = {}
                    for item in st.session_state.preguntas_examen:
                        if not item.get("tabla_origen"):
                            continue
                        clave = item["clave"]
                        correcta = st.session_state.plantilla_respuestas.get(clave)
                        if not correcta:
                            continue  # sin respuesta correcta cargada, no se puede evaluar
                        marcada = st.session_state.respuestas_usuario.get(clave)
                        # ✅ Solo se suben preguntas FALLADAS (marcada y distinta de la correcta).
                        # Las dejadas en blanco (marcada is None) NO se añaden al banco de errores.
                        if marcada is not None and marcada != correcta:
                            fallos_por_tabla.setdefault(item["tabla_origen"], []).append(item["numero"])

                    total_subidos = 0
                    errores_subida = []
                    for tabla_origen, numeros in fallos_por_tabla.items():
                        subidos, err = registrar_errores_en_supabase(tabla_origen, numeros)
                        if err:
                            errores_subida.append(err)
                        else:
                            total_subidos += subidos

                    if errores_subida:
                        st.error("⚠️ Hubo problemas subiendo algunos errores: " + "; ".join(errores_subida))
                    elif total_subidos:
                        st.success(f"✅ {total_subidos} pregunta(s) añadidas a tu Test de errores.")
                    else:
                        st.info("🎉 No has fallado ninguna pregunta con respuesta correcta cargada, nada que añadir.")

        # 📊 RESUMEN DE NOTAS (MEJORADO)
        if st.session_state.corregido and st.session_state.preguntas_examen:
            st.markdown("---")
            st.subheader("📊 Resultados del Examen")
            
            total_preguntas = len(st.session_state.preguntas_examen)
            
            # ✅ SOLO las preguntas que están en el examen cargado (comparamos por "clave",
            # que combina tabla_origen+numero para no confundir preguntas de distintas tablas
            # con el mismo número, como puede pasar en el Test de errores)
            numeros_examen = {p.get("clave") or str(p["numero"]) for p in st.session_state.preguntas_examen}
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
            
            # Aviso si faltan respuestas (mostramos el número real de pregunta, no la "clave" interna)
            claves_sin_plantilla = [n for n in numeros_examen if n not in plantilla_filtrada]
            if claves_sin_plantilla:
                numeros_sin_plantilla = sorted(
                    item["numero"] for item in st.session_state.preguntas_examen
                    if (item.get("clave") or str(item["numero"])) in claves_sin_plantilla
                )
                st.info(f"ℹ️ Preguntas sin respuesta correcta cargada: {', '.join(str(n) for n in numeros_sin_plantilla)}")

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