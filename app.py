import streamlit as st
import streamlit.components.v1 as components
import json
import os
import base64
import re
import time
import random
from datetime import datetime, timezone, timedelta
from io import BytesIO
import pandas as pd
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
    "subalterna",
    #"Aplicaciones_teorica_Parte1",
    #"Aplicaciones_teorica_Parte2",
    #"Sistemas_practica",
    #"Sistemas_teorica",
    #"Bateria_aplicaciones"
]

# --- CONSTANTES DEL SISTEMA DE DOMINIO / REPASO ---
REPASO_INTELIGENTE = "🧠 Repaso inteligente"
REPASO_ROJAS = "🔴 Test de rojas (no las sabía)"
REPASO_VERDES = "🟢 Test de verdes (comprobar)"

# Repasos de un solo color: modo -> estado de dominio que se selecciona
REPASOS_POR_COLOR = {
    REPASO_ROJAS: "rojo",
    REPASO_VERDES: "verde",
}
NOMBRE_COLOR = {"rojo": "rojas", "verde": "verdes"}

DESCRIPCION_REPASO = {
    REPASO_INTELIGENTE: "Prioriza rojas, incluye algunas verdes para comprobar que las dominas "
                        "y sube la prioridad de lo que lleva tiempo sin practicarse.",
    REPASO_ROJAS: "Solo preguntas rojas: las que has fallado o dejado en blanco.",
    REPASO_VERDES: "Solo preguntas verdes: las que dominas, para comprobar que no las has olvidado.",
}

# --- REGLAS DEL RANKING DE MÁS FALLADAS ---
MIN_FALLOS_MAS_FALLADAS = 3      # una pregunta solo aparece a partir de este nº de fallos acumulados
RACHA_ACIERTOS_PARA_QUITAR = 5   # si tus últimos N intentos de esa pregunta son todos aciertos, sale del ranking

COLORES_ESTADO = ["#2ecc71", "#e74c3c", "#95a5a6"]  # verde, rojo, sin practicar


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


def cargar_preguntas_desde_supabase(nombre_tabla, seleccion_texto="", orden="Orden numérico", n_aleatorias=None):
    """Recupera las preguntas guardadas en una tabla de Supabase, incluyendo la respuesta
    correcta (columna 'respuesta') para poder corregir sin subir una plantilla aparte.
    Permite filtrar por número de pregunta (rangos o individuales) y elegir el orden
    de presentación: numérico, aleatorio, o aleatorio con las opciones también desordenadas.
    Si n_aleatorias se indica junto a un orden aleatorio, se eligen N preguntas al azar
    de entre las seleccionadas (ej: rango 40-80 con 5 -> salen 5 al azar de ese rango)."""
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

        # 🎲 NUEVO: si hay orden aleatorio y se pidió un número concreto, muestreamos N al azar
        es_orden_aleatorio = orden in ("Orden aleatorio", "Orden aleatorio y respuestas desordenadas")
        if es_orden_aleatorio and n_aleatorias:
            filas_filtradas = random.sample(filas_filtradas, min(n_aleatorias, len(filas_filtradas)))

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

        contar_errores_pendientes.clear()
        return len(numeros_fallados), None
    except Exception as e:
        return 0, str(e)


@st.cache_data(ttl=300, show_spinner=False)
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
        contar_errores_pendientes.clear()
        return None
    except Exception as e:
        return str(e)


# =====================================================================
# MARCADOR DE PREGUNTAS (bookmark): marcar, desmarcar y cargar
# =====================================================================
def obtener_preguntas_marcadas():
    """Devuelve (conjunto de (tabla_origen, numero), error) con todas las preguntas marcadas."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        filas = supabase_client.table("preguntas_marcadas").select("tabla_origen, numero").execute().data
        return {(f["tabla_origen"], f["numero"]) for f in filas}, None
    except Exception as e:
        return None, str(e)


def alternar_marca_pregunta(tabla_origen, numero):
    """Marca o desmarca una pregunta (toggle) en 'preguntas_marcadas'.
    Devuelve (ahora_esta_marcada, error)."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        existe = supabase_client.table("preguntas_marcadas").select("id") \
            .eq("tabla_origen", tabla_origen).eq("numero", numero).execute().data
        if existe:
            supabase_client.table("preguntas_marcadas").delete() \
                .eq("tabla_origen", tabla_origen).eq("numero", numero).execute()
            return False, None
        supabase_client.table("preguntas_marcadas").insert({
            "tabla_origen": tabla_origen,
            "numero": numero,
            "marcada_en": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return True, None
    except Exception as e:
        return None, str(e)


def cargar_preguntas_marcadas():
    """Recupera todas las preguntas marcadas en 'preguntas_marcadas', trae el
    contenido ACTUAL desde cada tabla original y devuelve (preguntas, plantilla, error)."""
    if supabase_client is None:
        return None, None, "Supabase no está configurado."
    try:
        filas = supabase_client.table("preguntas_marcadas").select("tabla_origen, numero").execute().data
        if not filas:
            return [], {}, None

        por_tabla = {}
        for fila in filas:
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
        return None, None, f"No se pudieron cargar las preguntas marcadas desde Supabase: {e}"


# =====================================================================
# LECTURA PAGINADA Y CACHEADA (banco de preguntas, dominio e historial)
# =====================================================================
def _leer_tabla_completa(tabla, columnas, orden, pagina=1000):
    """Lee TODAS las filas de una tabla paginando (Supabase devuelve como máximo 1000 por consulta)."""
    filas = []
    desde = 0
    while True:
        datos = supabase_client.table(tabla).select(columnas).order(orden) \
            .range(desde, desde + pagina - 1).execute().data
        filas.extend(datos)
        if len(datos) < pagina:
            break
        desde += pagina
    return filas


@st.cache_data(ttl=600, show_spinner=False)
def _pool_preguntas_cacheado():
    """Lista de referencias (tabla, numero) de todo el banco. Solo trae la columna 'numero'."""
    pool = []
    for tabla in TABLAS_SUPABASE_DISPONIBLES:
        for fila in _leer_tabla_completa(tabla, "numero", "numero"):
            if fila.get("numero") is not None:
                pool.append((tabla, fila["numero"]))
    return pool


def obtener_pool_preguntas():
    """Devuelve (pool, error). Los errores no se cachean, así que se reintenta en la siguiente llamada."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        return _pool_preguntas_cacheado(), None
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=120, show_spinner=False)
def _estados_dominio_cacheados():
    filas = _leer_tabla_completa(
        "dominio_preguntas", "tabla_origen, numero, estado, veces_practicada, ultima_practica", "id"
    )
    return {
        (f["tabla_origen"], f["numero"]): {
            "estado": f["estado"],
            "veces": f.get("veces_practicada") or 0,
            "ultima": f.get("ultima_practica"),
        }
        for f in filas
    }


def obtener_estados_dominio():
    """Devuelve ({(tabla, numero): {estado, veces, ultima}}, error)."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        return _estados_dominio_cacheados(), None
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=300, show_spinner=False)
def _historial_cacheado():
    return _leer_tabla_completa("historial_dominio", "tabla_origen, numero, estado, fecha", "id")


def obtener_historial():
    """Devuelve (lista_de_intentos, error). Si la tabla opcional 'historial_dominio' no existe -> error."""
    if supabase_client is None:
        return None, "Supabase no está configurado."
    try:
        return _historial_cacheado(), None
    except Exception as e:
        return None, str(e)


def _limpiar_caches_dominio():
    _estados_dominio_cacheados.clear()
    _historial_cacheado.clear()


# =====================================================================
# PREGUNTAS MÁS FALLADAS (ranking acumulado a lo largo del tiempo)
# =====================================================================
def calcular_mas_falladas():
    """Ranking acumulado de preguntas por nº de veces que han salido en ROJO (falladas o en blanco).
    Se calcula desde 'historial_dominio', que registra CADA intento de CADA test y nunca se borra.
    Reglas:
      - Solo entran preguntas con al menos MIN_FALLOS_MAS_FALLADAS fallos acumulados.
      - Salen del ranking si tus últimos RACHA_ACIERTOS_PARA_QUITAR intentos de esa pregunta fueron aciertos.
    Devuelve ([(tabla, numero, veces), ...] de más a menos falladas, error)."""
    historial, error = obtener_historial()
    if error:
        return None, error

    intentos = {}   # ref -> lista de (fecha, estado)
    for h in historial:
        ref = (h["tabla_origen"], h["numero"])
        intentos.setdefault(ref, []).append((str(h.get("fecha") or ""), h.get("estado")))

    cuentas = {}
    ultima = {}
    for ref, lista in intentos.items():
        lista.sort(key=lambda x: x[0])  # de más antiguo a más reciente
        fallos = sum(1 for _, est in lista if est == "rojo")
        if fallos < MIN_FALLOS_MAS_FALLADAS:
            continue
        ultimos = lista[-RACHA_ACIERTOS_PARA_QUITAR:]
        if len(ultimos) == RACHA_ACIERTOS_PARA_QUITAR and all(est == "verde" for _, est in ultimos):
            continue  # ya la dominas: 5 aciertos seguidos
        cuentas[ref] = fallos
        ultima[ref] = max((f for f, est in lista if est == "rojo"), default="")

    # Orden: más veces primero; a igualdad de veces, la fallada más recientemente primero
    refs = sorted(cuentas, key=lambda r: ultima.get(r, ""), reverse=True)
    refs.sort(key=lambda r: cuentas[r], reverse=True)
    return [(t, n, cuentas[(t, n)]) for (t, n) in refs], None


@st.cache_data(ttl=600, show_spinner=False)
def _contenido_preguntas_cacheado(referencias):
    """Contenido de una lista de referencias (tupla de (tabla, numero)), cacheado para que
    la pestaña no consulte Supabase en cada recarga."""
    return traer_preguntas_por_referencias(list(referencias))


def cargar_test_mas_falladas(n_preguntas):
    """Test con las N preguntas más falladas de todos los tiempos (ordenadas de más a menos falladas).
    Si hay menos de N preguntas con algún fallo, se usan las que haya."""
    if supabase_client is None:
        return None, None, 0, "Supabase no está configurado."
    try:
        ranking, error = calcular_mas_falladas()
        if error:
            return None, None, 0, f"No se pudo leer tu historial: {error}"
        if not ranking:
            return [], {}, 0, None
        top = ranking[:n_preguntas]
        refs = [(t, n) for t, n, _ in top]
        preguntas, plantilla = traer_preguntas_por_referencias(refs)
        posicion = {ref: i for i, ref in enumerate(refs)}
        preguntas.sort(key=lambda p: posicion.get((p["tabla_origen"], p["numero"]), 10**9))
        return preguntas, plantilla, len(preguntas), None
    except Exception as e:
        return None, None, 0, f"No se pudo generar el Test de más falladas: {e}"



# =====================================================================
# CARGA DE PREGUNTAS POR REFERENCIA (tabla, numero)
# =====================================================================
def traer_preguntas_por_referencias(referencias):
    """Dada una lista de (tabla, numero), trae el contenido completo con una sola consulta por tabla."""
    por_tabla = {}
    for tabla, numero in referencias:
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
    return preguntas, plantilla_respuestas


# --- TEST MIXTO: N PREGUNTAS AL AZAR, PROPORCIONAL AL TAMAÑO DE CADA TABLA ---
def cargar_test_mixto(n_preguntas):
    """Genera un examen mezclando N preguntas al azar de TODAS las tablas de
    TABLAS_SUPABASE_DISPONIBLES. El azar es 'puro' sobre el conjunto combinado de todas
    las preguntas existentes, así que las tablas más grandes aportan proporcionalmente
    más preguntas (igual que un examen real mezclado por temarios de distinto peso)."""
    if supabase_client is None:
        return None, None, 0, "Supabase no está configurado."
    try:
        # 1. Pool ligero (solo referencias tabla+numero), paginado y cacheado.
        pool, error_pool = obtener_pool_preguntas()
        if error_pool:
            return None, None, 0, f"No se pudo generar el Test Mixto desde Supabase: {error_pool}"

        if not pool:
            return [], {}, 0, None

        n_final = min(n_preguntas, len(pool))
        seleccionados = random.sample(pool, n_final)

        # 2. Contenido completo con una consulta por tabla implicada
        preguntas, plantilla_respuestas = traer_preguntas_por_referencias(seleccionados)

        # 3. Mezclamos el orden final para que no salgan agrupadas por tabla
        random.shuffle(preguntas)

        return preguntas, plantilla_respuestas, n_final, None
    except Exception as e:
        return None, None, 0, f"No se pudo generar el Test Mixto desde Supabase: {e}"


# =====================================================================
# SISTEMA DE DOMINIO: SELECCIÓN DE PREGUNTAS PARA EL REPASO
# =====================================================================
def _parsear_fecha(valor):
    """Convierte un timestamptz de Supabase en datetime con zona horaria (tolerante a formatos)."""
    if not valor:
        return None
    try:
        s = str(valor).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Normaliza fracciones de segundo irregulares (p. ej. 5 dígitos) a 6
            s2 = re.sub(r'\.(\d+)', lambda m: '.' + (m.group(1) + '000000')[:6], s, count=1)
            dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _dias_desde(valor):
    dt = _parsear_fecha(valor)
    if dt is None:
        return 30.0
    return max((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 0.0)


def _peso_antiguedad(info):
    """Peso dentro de su color: cuanto más tiempo sin practicar, más prioridad.
    Si se practicó hace menos de un día, se reduce para evitar repetir siempre lo mismo."""
    if not info:
        return 1.0
    dias = _dias_desde(info.get("ultima"))
    peso = 1.0 + min(dias, 30.0) / 10.0
    if dias < 1:
        peso *= 0.5
    return peso


def _muestreo_ponderado(candidatos, k, estados):
    """Muestreo aleatorio SIN reemplazo ponderado por antigüedad (Efraimidis-Spirakis)."""
    if k <= 0 or not candidatos:
        return []
    puntuadas = []
    for ref in candidatos:
        peso = max(_peso_antiguedad(estados.get(ref)), 1e-6)
        puntuadas.append((random.random() ** (1.0 / peso), ref))
    puntuadas.sort(key=lambda x: x[0], reverse=True)
    return [ref for _, ref in puntuadas[:k]]


def _repartir_por_cuotas(grupos, cuotas, prioridad, n, estados):
    """Elige n preguntas repartiendo por cuotas entre colores; si un color no tiene suficientes,
    el sobrante se rellena siguiendo el orden de prioridad."""
    disponibles_total = sum(len(grupos[c]) for c in cuotas)
    n = min(n, disponibles_total)
    if n <= 0:
        return []

    objetivo = {c: int(n * p) for c, p in cuotas.items()}
    resto = n - sum(objetivo.values())
    i = 0
    while resto > 0:
        objetivo[prioridad[i % len(prioridad)]] += 1
        resto -= 1
        i += 1

    elegidas = []
    tomadas = {}
    for c in cuotas:
        sel = _muestreo_ponderado(grupos[c], min(objetivo[c], len(grupos[c])), estados)
        tomadas[c] = set(sel)
        elegidas.extend(sel)

    falta = n - len(elegidas)
    for c in prioridad:
        if falta <= 0:
            break
        restantes = [r for r in grupos[c] if r not in tomadas[c]]
        sel = _muestreo_ponderado(restantes, falta, estados)
        elegidas.extend(sel)
        falta -= len(sel)

    random.shuffle(elegidas)
    return elegidas


def seleccionar_referencias_repaso(modo, n, pool, estados):
    """Devuelve (lista de (tabla, numero), aviso). Solo se consideran preguntas que existen en el banco."""
    grupos = {"rojo": [], "verde": [], "nuevo": []}
    for ref in pool:
        info = estados.get(ref)
        grupos[info["estado"] if info and info.get("estado") in grupos else "nuevo"].append(ref)

    hay_estados = bool(grupos["rojo"] or grupos["verde"])

    # Repaso de un solo color (rojas / amarillas / verdes)
    if modo in REPASOS_POR_COLOR:
        color = REPASOS_POR_COLOR[modo]
        candidatos = grupos[color]
        if not candidatos:
            return [], f"No tienes preguntas {NOMBRE_COLOR[color]} ahora mismo."
        refs = _muestreo_ponderado(candidatos, min(n, len(candidatos)), estados)
        random.shuffle(refs)
        return refs, None

    if modo == REPASO_INTELIGENTE:
        cuotas = {"rojo": 0.50, "verde": 0.20, "nuevo": 0.30}
        prioridad = ["rojo", "nuevo", "verde"]
        refs = _repartir_por_cuotas(grupos, cuotas, prioridad, n, estados)
        aviso = None if hay_estados else "Aún no tienes estados registrados en esta tabla: el test es aleatorio."
        return refs, aviso


def cargar_test_repaso(modo, n_preguntas, tabla):
    """Genera un test de repaso sobre UNA tabla. Devuelve (preguntas, plantilla, n_generadas, aviso, error)."""
    if supabase_client is None:
        return None, None, 0, None, "Supabase no está configurado."

    pool_total, error_pool = obtener_pool_preguntas()
    if error_pool:
        return None, None, 0, None, f"No se pudo leer el banco de preguntas: {error_pool}"
    pool = [ref for ref in pool_total if ref[0] == tabla]
    if not pool:
        return [], {}, 0, f"La tabla '{tabla}' no tiene preguntas.", None

    estados, error_estados = obtener_estados_dominio()
    if error_estados:
        return None, None, 0, None, (
            f"No se pudo leer tu progreso (¿existe la tabla 'dominio_preguntas'?): {error_estados}"
        )

    refs, aviso = seleccionar_referencias_repaso(modo, n_preguntas, pool, estados)
    if not refs:
        return [], {}, 0, aviso, None

    try:
        preguntas, plantilla = traer_preguntas_por_referencias(refs)
    except Exception as e:
        return None, None, 0, None, f"No se pudo cargar el contenido de las preguntas: {e}"

    random.shuffle(preguntas)
    return preguntas, plantilla, len(preguntas), aviso, None


# =====================================================================
# SISTEMA DE DOMINIO: CÁLCULO Y GUARDADO DEL ESTADO
# =====================================================================
def calcular_estado(marcada, correcta):
    """Correcta = verde. Incorrecta o en blanco = rojo.
    Sin respuesta correcta disponible -> None (no se guarda nada)."""
    if not correcta:
        return None
    if marcada is None or marcada != correcta:
        return "rojo"
    return "verde"


def guardar_dominio_test(preguntas, plantilla, respuestas):
    """Actualiza 'dominio_preguntas' con el resultado del test. Devuelve (resumen, error).
    - Solo preguntas con tabla_origen (las de PDF suelto se ignoran) y con respuesta correcta cargada.
    - Una única operación upsert por (tabla_origen, numero): nunca duplica registros.
    - veces_practicada = valor actual en BD + 1 (se lee fresco, no de la caché)."""
    if supabase_client is None:
        return None, None

    nuevos = {}
    for it in preguntas:
        tabla = it.get("tabla_origen")
        if not tabla:
            continue
        k = it["clave"]
        estado = calcular_estado(respuestas.get(k), plantilla.get(k))
        if estado is None:
            continue
        nuevos[(tabla, it["numero"])] = estado

    if not nuevos:
        return None, None  # nada que guardar (PDF suelto o sin respuestas correctas)

    try:
        por_tabla = {}
        for tabla, numero in nuevos:
            por_tabla.setdefault(tabla, []).append(numero)

        previas = {}
        for tabla, numeros in por_tabla.items():
            datos = supabase_client.table("dominio_preguntas") \
                .select("numero, veces_practicada") \
                .eq("tabla_origen", tabla).in_("numero", numeros).execute().data
            for f in datos:
                previas[(tabla, f["numero"])] = f.get("veces_practicada") or 0

        ahora = datetime.now(timezone.utc).isoformat()
        filas = [
            {
                "tabla_origen": tabla,
                "numero": numero,
                "estado": estado,
                "veces_practicada": previas.get((tabla, numero), 0) + 1,
                "ultima_practica": ahora,
            }
            for (tabla, numero), estado in nuevos.items()
        ]
        supabase_client.table("dominio_preguntas").upsert(filas, on_conflict="tabla_origen,numero").execute()
    except Exception as e:
        return None, str(e)

    # Historial opcional (tabla 'historial_dominio'): si no existe, se desactiva sin molestar.
    if not st.session_state.get("historial_no_disponible"):
        try:
            supabase_client.table("historial_dominio").insert([
                {"tabla_origen": t, "numero": n, "estado": e, "fecha": ahora}
                for (t, n), e in nuevos.items()
            ]).execute()
        except Exception:
            st.session_state["historial_no_disponible"] = True

    _limpiar_caches_dominio()
    resumen = {
        "n": len(nuevos),
        "verde": sum(1 for e in nuevos.values() if e == "verde"),
        "rojo": sum(1 for e in nuevos.values() if e == "rojo"),
    }
    return resumen, None


def ejecutar_guardado_dominio():
    """Guarda el dominio del test actual UNA sola vez (si ya se guardó bien, no repite)."""
    resultado_previo = st.session_state.get("dominio_resultado")
    if resultado_previo and resultado_previo.get("ok"):
        return
    resumen, error = guardar_dominio_test(
        st.session_state.preguntas_examen,
        st.session_state.plantilla_respuestas,
        st.session_state.respuestas_usuario,
    )
    if error:
        st.session_state.dominio_resultado = {"ok": False, "error": error}
    elif resumen:
        st.session_state.dominio_resultado = {"ok": True, "resumen": resumen}
    else:
        st.session_state.dominio_resultado = None


st.set_page_config(page_title=" Oposiciones - Escaneados Pro", layout="wide")
#st.title("🛡️ Sistema Inteligente de Exámenes (Soporta PDFs Escaneados)")

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
    st.session_state.idx_pregunta = 0
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
if "idx_pregunta" not in st.session_state:
    st.session_state.idx_pregunta = 0
# Sistema de dominio: resultado del guardado
if "dominio_resultado" not in st.session_state:
    st.session_state.dominio_resultado = None
# Marcador de preguntas: conjunto de (tabla_origen, numero) marcadas con 🔖
if "marcadas_set" not in st.session_state:
    st.session_state.marcadas_set = set()
if "marcadas_cargadas" not in st.session_state:
    st.session_state["marcadas_cargadas"] = False

# Cargamos el marcador desde Supabase una sola vez por sesión
if supabase_client is not None and not st.session_state["marcadas_cargadas"]:
    _marcadas, _err_marcadas = obtener_preguntas_marcadas()
    if _marcadas is not None:
        st.session_state.marcadas_set = _marcadas
        st.session_state["marcadas_cargadas"] = True


def reiniciar_intento():
    """Deja a cero un intento (respuestas, corrección y resultado de dominio)."""
    st.session_state.respuestas_usuario = {}
    st.session_state.dominio_resultado = None
    st.session_state.corregido = False
    st.session_state.idx_pregunta = 0


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
    st.header("🎯 Modo de estudio")
    st.radio(
        "modo_estudio_radio",
        options=["Modo normal", "Modo examen"],
        key="modo_estudio",
        label_visibility="collapsed",
        help="Normal: te dice al momento si has acertado (verde) o fallado (rojo) y cuál era la correcta. "
             "Examen: no ves nada hasta terminar todas las preguntas; entonces sale la nota y el resumen de fallos."
    )
    st.caption("📖 **Normal**: corrección al instante en cada pregunta.  \n📝 **Examen**: resultados solo al final.")
    st.markdown("---")

    st.header("🗄️ Base de datos (Supabase)")
    if supabase_client is None:
        st.caption("⚠️ Supabase no configurado. Añade SUPABASE_URL y SUPABASE_KEY al .env para activarlo.")
    tabla_seleccionada = st.selectbox(
        "Tabla / temario a usar:",
        options=TABLAS_SUPABASE_DISPONIBLES,
        key="tabla_supabase_actual",
        help="Cada tabla guarda las preguntas de un temario o parte distinta del examen."
    )

    # ---------- REPASO POR DOMINIO (sobre la tabla elegida) ----------
    st.markdown("**🎯 Repasar preguntas de esta tabla:**")
    tipo_repaso = st.radio(
        "tipo_repaso_radio",
        options=[REPASO_INTELIGENTE, REPASO_ROJAS, REPASO_VERDES],
        key="tipo_repaso",
        label_visibility="collapsed",
    )
    st.caption(DESCRIPCION_REPASO[tipo_repaso])
    n_preguntas_repaso = st.number_input(
        "¿Cuántas preguntas quieres? (20, 30, 50, 100...)",
        min_value=1, max_value=500, value=30, step=5,
        key="n_preguntas_repaso"
    )
    if supabase_client is None:
        st.caption("⚠️ El repaso necesita Supabase configurado.")
    elif st.button("🚀 Empezar repaso", type="primary"):
        with st.spinner(f"Seleccionando preguntas de '{tabla_seleccionada}'..."):
            preguntas_rep, plantilla_rep, n_gen_rep, aviso_rep, error_rep = cargar_test_repaso(
                tipo_repaso, n_preguntas_repaso, tabla_seleccionada
            )
            if error_rep:
                st.error(error_rep)
            elif preguntas_rep:
                st.session_state.preguntas_examen = preguntas_rep
                reiniciar_intento()
                st.session_state.plantilla_respuestas = plantilla_rep
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = False
                mensaje = f"✅ {tipo_repaso} en '{tabla_seleccionada}': {n_gen_rep} pregunta(s) listas"
                if n_gen_rep < n_preguntas_repaso:
                    mensaje += f" (pediste {n_preguntas_repaso}, solo hay {n_gen_rep} disponibles con este criterio)"
                st.success(mensaje + ".")
                if aviso_rep:
                    st.info(aviso_rep)
            else:
                st.info(aviso_rep or "No se encontraron preguntas para este tipo de repaso.")
    st.caption("📈 Tu evolución está en la pestaña **Progreso**.")
    st.markdown("---")
    st.markdown("**📋 O elige preguntas por número / rango:**")

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

    # 🎲 NUEVO: número de preguntas aleatorias sobre la selección (solo con orden aleatorio)
    n_aleatorias_rango = None
    if orden_preguntas != "Orden numérico":
        usar_n_aleatorias = st.checkbox(
            "🎲 Elegir solo N preguntas al azar de la selección",
            key="usar_n_aleatorias",
            help="Ej: rango 40-80 con 5 preguntas -> salen 5 elegidas al azar de ese rango (p. ej. 45, 72, 49...)"
        )
        if usar_n_aleatorias:
            n_aleatorias_rango = st.number_input(
                "¿Cuántas preguntas aleatorias quieres?",
                min_value=1, max_value=500, value=10, step=1,
                key="n_aleatorias_rango"
            )

    if supabase_client is not None and st.button("☁️ Cargar preguntas guardadas de esta tabla"):
        with st.spinner(f"Consultando '{tabla_seleccionada}' en Supabase..."):
            preguntas_supabase, plantilla_supabase, error_supabase = cargar_preguntas_desde_supabase(
                tabla_seleccionada, seleccion_preguntas_texto, orden_preguntas, n_aleatorias_rango
            )
            if error_supabase:
                st.error(error_supabase)
            elif preguntas_supabase:
                st.session_state.preguntas_examen = preguntas_supabase
                reiniciar_intento()
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = False
                # ✅ Comparamos directamente contra la respuesta guardada en la BD, sin necesidad
                # de subir una plantilla de respuestas aparte.
                st.session_state.plantilla_respuestas = plantilla_supabase
                mensaje = f"✅ Cargadas {len(preguntas_supabase)} preguntas desde Supabase"
                if n_aleatorias_rango and len(preguntas_supabase) < n_aleatorias_rango:
                    mensaje += f" (pediste {n_aleatorias_rango}, pero solo había {len(preguntas_supabase)} en esa selección)"
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
                reiniciar_intento()
                st.session_state.plantilla_respuestas = plantilla_error
                st.session_state.modo_test_errores = True
                st.session_state.modo_test_mixto = False
                st.success(f"✅ Cargado Test de errores con {len(preguntas_error)} pregunta(s) pendientes.")
            else:
                st.info("🎉 ¡No tienes errores guardados! Tu banco de errores está vacío.")

    st.header("🔥 Test de más falladas")
    st.caption(
        "Las preguntas que más veces has fallado a lo largo del tiempo (acumulado de todos tus tests). "
        f"Entran a partir de {MIN_FALLOS_MAS_FALLADAS} fallos y salen si aciertas las últimas {RACHA_ACIERTOS_PARA_QUITAR} veces seguidas. "
        "Distinto del test de rojas, que solo mira el estado actual."
    )
    if supabase_client is not None:
        _rank_sb, _err_rank_sb = calcular_mas_falladas()
        if _rank_sb:
            st.caption(f"📌 Tienes **{len(_rank_sb)}** pregunta(s) en tu ranking de más falladas.")
        elif _err_rank_sb is None:
            st.caption("📌 Ninguna pregunta cumple todavía las condiciones del ranking.")
    n_preguntas_falladas = st.number_input(
        "¿Cuántas de las más falladas quieres?",
        min_value=1, max_value=500, value=20, step=1,
        key="n_preguntas_mas_falladas"
    )
    if supabase_client is not None and st.button("🔥 Cargar Test de más falladas"):
        with st.spinner("Buscando tus preguntas más falladas..."):
            preguntas_ff, plantilla_ff, n_ff, error_ff = cargar_test_mas_falladas(n_preguntas_falladas)
            if error_ff:
                st.error(error_ff)
            elif preguntas_ff:
                st.session_state.preguntas_examen = preguntas_ff
                reiniciar_intento()
                st.session_state.plantilla_respuestas = plantilla_ff
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = False
                mensaje = f"✅ Test de más falladas con {n_ff} pregunta(s)"
                if n_ff < n_preguntas_falladas:
                    mensaje += f" (pediste {n_preguntas_falladas}, pero solo hay {n_ff} que cumplan las condiciones)"
                st.success(mensaje + ".")
            else:
                st.info("🎉 Ninguna pregunta cumple todavía las condiciones del ranking (3 fallos o más y sin 5 aciertos seguidos al final).")

    st.header("🔖 Preguntas marcadas")
    st.caption("Marca preguntas con el icono 🔖 mientras las respondes y vuelve a ellas cuando quieras.")
    if supabase_client is not None:
        n_marcadas = len(st.session_state.marcadas_set)
        if n_marcadas > 0:
            st.caption(f"📌 Tienes **{n_marcadas}** pregunta(s) marcada(s).")
        else:
            st.caption("📌 No tienes preguntas marcadas todavía.")
    if supabase_client is not None and st.button("🔖 Cargar Test de marcadas"):
        with st.spinner("Consultando tus preguntas marcadas en Supabase..."):
            preguntas_marc, plantilla_marc, error_marc = cargar_preguntas_marcadas()
            if error_marc:
                st.error(error_marc)
            elif preguntas_marc:
                st.session_state.preguntas_examen = preguntas_marc
                reiniciar_intento()
                st.session_state.plantilla_respuestas = plantilla_marc
                st.session_state.modo_test_errores = False
                st.session_state.modo_test_mixto = False
                st.success(f"✅ Test de marcadas cargado con {len(preguntas_marc)} pregunta(s).")
            else:
                st.info("🔖 No tienes preguntas marcadas. Usa el icono de la esquina de cada pregunta.")

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
                reiniciar_intento()
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
                # añadirse al banco de errores ni al sistema de dominio (no hay tabla+numero de referencia).
                for p in preguntas_parseadas:
                    p["tabla_origen"] = None
                    p["clave"] = str(p.get("numero"))

                if preguntas_parseadas and len(preguntas_parseadas) > 0:
                    st.session_state.preguntas_examen = preguntas_parseadas
                    reiniciar_intento()
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


# --- Los fragmentos permiten que al clicar una opción solo se recargue la pregunta, no toda la app ---
_fragmento = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)

def _rerun_zona():
    """Recarga solo el fragmento (rápido). Si la versión de Streamlit no lo soporta, recarga todo."""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()

def _rerun_app():
    try:
        st.rerun(scope="app")
    except TypeError:
        st.rerun()

def _decorar(f):
    return _fragmento(f) if _fragmento else f

@_decorar
def render_pregunta(modo_examen, mostrar_origen):
    preguntas = st.session_state.preguntas_examen
    total = len(preguntas)
    idx = min(max(st.session_state.idx_pregunta, 0), total - 1)
    st.session_state.idx_pregunta = idx
    item = preguntas[idx]
    clave = item.get("clave") or str(item["numero"])
    marcada = st.session_state.respuestas_usuario.get(clave)
    correcta = st.session_state.plantilla_respuestas.get(clave)

    st.caption(("📝 MODO EXAMEN" if modo_examen else "📖 MODO NORMAL") + f"  ·  Pregunta {idx + 1} de {total}  ·  ⌨️ 1-4 responder · ← → navegar")
    st.progress((idx + 1) / total)

    etiqueta_origen = f" _(de {item['tabla_origen']})_" if mostrar_origen and item.get("tabla_origen") else ""
    ref_marca = (item.get("tabla_origen"), item["numero"])
    es_marcada = ref_marca in st.session_state.marcadas_set

    # Título + botón de marcar (como el marcador de la esquina en tu captura)
    hcol1, hcol2 = st.columns([0.78, 0.22])
    with hcol1:
        st.markdown(f"#### Pregunta {item['numero']}{etiqueta_origen}")
    with hcol2:
        if st.button(
            "✔ Marcada" if es_marcada else "Marcar",
            key=f"marca_{clave}",
            type="primary" if es_marcada else "secondary",
            help="Marcar/desmarcar esta pregunta" if item.get("tabla_origen")
                 else "Solo se pueden marcar preguntas cargadas desde Supabase",
            use_container_width=True,
        ):
            if not item.get("tabla_origen"):
                st.toast("⚠️ Solo se pueden marcar preguntas cargadas desde Supabase.")
            else:
                ahora_marcada, err_marca = alternar_marca_pregunta(*ref_marca)
                if err_marca:
                    st.toast(f"⚠️ Error al guardar la marca: {err_marca}")
                else:
                    if ahora_marcada:
                        st.session_state.marcadas_set.add(ref_marca)
                        st.toast("🔖 Pregunta marcada.")
                    else:
                        st.session_state.marcadas_set.discard(ref_marca)
                        st.toast("📑 Marca eliminada.")
                    _rerun_zona()
    st.markdown(f"**{item['pregunta']}**")

    # En modo normal, al contestar se bloquea la pregunta y se muestra la corrección al momento.
    bloqueada = (not modo_examen) and marcada is not None
    revelar = bloqueada  # en modo normal la corrección sale al instante

    for opt in item["opciones"]:
        letra = str(opt["letra"]).lower()
        texto_opt = f"{letra.upper()}) {opt['texto']}"
        if revelar:
            if correcta and letra == correcta:
                st.success(texto_opt, icon="✅")
            elif letra == marcada:
                if correcta:
                    st.error(texto_opt, icon="❌")
                else:
                    st.info(texto_opt, icon="⚪")
            else:
                with st.container(border=True):
                    st.markdown(texto_opt)
        elif bloqueada:
            # Esperando la autoevaluación: se marca la elegida sin revelar la correcta
            if letra == marcada:
                st.info(texto_opt, icon="⚪")
            else:
                with st.container(border=True):
                    st.markdown(texto_opt)
        else:
            es_marcada = (marcada == letra)
            if st.button(
                texto_opt,
                key=f"opt_{clave}_{letra}",
                use_container_width=True,
                type="primary" if (modo_examen and es_marcada) else "secondary",
            ):
                st.session_state.respuestas_usuario[clave] = letra
                _rerun_zona()

    if modo_examen and marcada is not None:
        if st.button("✖️ Borrar mi respuesta", key=f"borrar_{clave}"):
            del st.session_state.respuestas_usuario[clave]
            _rerun_zona()

    st.markdown("---")
    es_ultima = (idx == total - 1)
    nav1, nav2 = st.columns(2)
    with nav1:
        if st.button("‹ Anterior", disabled=(idx == 0), use_container_width=True):
            st.session_state.idx_pregunta = idx - 1
            _rerun_zona()
    with nav2:
        if not es_ultima:
            destacar = modo_examen or marcada is not None
            if st.button("Siguiente ›", type="primary" if destacar else "secondary",
                         use_container_width=True):
                st.session_state.idx_pregunta = idx + 1
                _rerun_zona()
        else:
            if st.button("🏁 Finalizar y ver resultados", type="primary", use_container_width=True):
                st.session_state.corregido = True

                # Sistema de dominio: actualiza dominio_preguntas (independiente del banco de errores)
                ejecutar_guardado_dominio()

                # Test de errores: las ACERTADAS se borran del banco de errores
                if st.session_state.modo_test_errores:
                    aciertos_para_borrar = []
                    for it in preguntas:
                        k = it["clave"]
                        c = st.session_state.plantilla_respuestas.get(k)
                        m = st.session_state.respuestas_usuario.get(k)
                        if c and m == c:
                            aciertos_para_borrar.append((it["tabla_origen"], it["numero"]))
                    if aciertos_para_borrar:
                        error_borrado = borrar_errores_acertados(aciertos_para_borrar)
                        if error_borrado:
                            st.toast(f"⚠️ No se pudieron borrar algunos errores: {error_borrado}")
                        else:
                            st.toast(f"🗑️ {len(aciertos_para_borrar)} pregunta(s) acertada(s) borradas de tu banco de errores.")
                _rerun_app()

    sin_contestar = sum(1 for it in preguntas if (it.get("clave") or str(it["numero"])) not in st.session_state.respuestas_usuario)
    if es_ultima and sin_contestar:
        st.caption(f"⚠️ Te quedan {sin_contestar} pregunta(s) sin contestar (contarán como en blanco).")


ATAJOS_TECLADO_JS = """
<script>
(function () {
  const w = window.parent, doc = w.document;
  if (w.__opoKeys) doc.removeEventListener('keydown', w.__opoKeys);
  w.__opoKeys = function (e) {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const zona = doc.querySelector('[data-testid="stMain"]') || doc.querySelector('section.main') || doc;
    const botones = Array.from(zona.querySelectorAll('button'));
    const buscar = f => botones.find(b => f((b.innerText || '').trim()));
    let b = null;
    if (['1', '2', '3', '4'].includes(e.key)) {
      // Las opciones pueden estar desordenadas: 1-4 responde la opción que se ve
      // en esa posición (la 1ª de arriba, la 2ª...), sin importar su letra.
      const opciones = botones.filter(bb => /^[A-D][)]/.test((bb.innerText || '').trim()));
      b = opciones[parseInt(e.key) - 1];
    } else if (e.key === 'ArrowRight') {
      b = buscar(txt => txt.includes('Siguiente'));
    } else if (e.key === 'ArrowLeft') {
      b = buscar(txt => txt.includes('Anterior'));
    }
    if (b && !b.disabled) { e.preventDefault(); b.click(); }
  };
  doc.addEventListener('keydown', w.__opoKeys);
})();
</script>
"""

# --- Estilo: tipografía limpia + opciones alineadas a la izquierda ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap');

/* Tipografía principal (se evitan los iconos de Streamlit para no romperlos) */
[data-testid="stMain"] .stMarkdown,
[data-testid="stMain"] .stMarkdown p,
[data-testid="stMain"] h1, [data-testid="stMain"] h2,
[data-testid="stMain"] h3, [data-testid="stMain"] h4,
[data-testid="stMain"] .stButton button,
[data-testid="stMain"] .stButton button p,
[data-testid="stMain"] [data-testid="stAlert"] p,
[data-testid="stMain"] [data-testid="stMetricValue"],
[data-testid="stMain"] [data-testid="stMetricLabel"] p,
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] label p {
    font-family: 'Plus Jakarta Sans', 'Segoe UI', sans-serif !important;
}

/* Enunciado en negrita, pero más ligero que el negro por defecto */
[data-testid="stMain"] .stMarkdown strong { font-weight: 700; }

/* Opciones: texto normal (no negrita), tamaño contenido */
[class*="st-key-opt_"] button p {
    font-weight: 500 !important;
    font-size: 0.95rem !important;
    line-height: 1.4 !important;
}

/* Etiqueta pequeña "PREGUNTA X DE Y" en monoespaciada, como en la web original */
[data-testid="stMain"] [data-testid="stCaptionContainer"],
[data-testid="stMain"] [data-testid="stCaptionContainer"] p {
    font-family: 'JetBrains Mono', ui-monospace, monospace !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

/* Todas las opciones empiezan en el mismo punto de la izquierda */
[class*="st-key-opt_"] button,
[class*="st-key-opt_"] button > div,
[class*="st-key-opt_"] button p {
    justify-content: flex-start !important;
    text-align: left !important;
}

/* Botón de marcar: AMARILLO llamativo cuando la pregunta está marcada */
[class*="st-key-marca_"] button[kind="primary"] {
    background-color: #f1c40f !important;
    border-color: #d4ac0d !important;
    color: #3d3000 !important;
    font-weight: 700 !important;
    box-shadow: 0 0 0 3px rgba(241, 196, 15, 0.35) !important;
}
[class*="st-key-marca_"] button[kind="primary"] p {
    color: #3d3000 !important;
    font-weight: 700 !important;
}
</style>
""", unsafe_allow_html=True)

# --- PANTALLA PRINCIPAL: PESTAÑAS ---
tab1, tab2, tab_progreso, tab_falladas, tab_marcadas, tab3 = st.tabs(
    ["📝 Cuestionario Interactivo", "💬 Chat con el Examen", "📈 Progreso", "🔥 Más falladas", "🔖 Marcadas", "🔧 Diagnóstico"]
)

with tab1:
    if st.session_state.preguntas_examen is None:
        st.info("👋 Sube tu examen en PDF en la barra lateral izquierda y pulsa 'Procesar Examen', "
                "o empieza un repaso desde **🎯 Repasar preguntas**.")
    else:
        preguntas = st.session_state.preguntas_examen
        total = len(preguntas)
        modo_examen = st.session_state.get("modo_estudio", "Modo normal") == "Modo examen"
        mostrar_origen = (st.session_state.modo_test_errores or st.session_state.modo_test_mixto)
        viene_de_supabase = any(item.get("tabla_origen") for item in preguntas)

        # PANTALLA 1: PREGUNTAS DE 1 EN 1 (mientras no se haya finalizado)
        if not st.session_state.corregido:
            render_pregunta(modo_examen, mostrar_origen)
            components.html(ATAJOS_TECLADO_JS, height=0)

        # PANTALLA 2: RESULTADOS Y RESUMEN DE FALLOS
        else:
            st.subheader("📊 Resultados")

            numeros_examen = {p.get("clave") or str(p["numero"]) for p in preguntas}
            plantilla_filtrada = {
                n: r for n, r in st.session_state.plantilla_respuestas.items() if n in numeros_examen
            }
            preguntas_con_respuesta = len(plantilla_filtrada)
            ur = st.session_state.respuestas_usuario

            aciertos = sum(1 for n, r in plantilla_filtrada.items() if ur.get(n) == r)
            fallos = sum(1 for n, r in plantilla_filtrada.items() if ur.get(n) is not None and ur.get(n) != r)
            blancos = sum(1 for n in plantilla_filtrada if ur.get(n) is None)

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("✅ Aciertos", aciertos)
            col2.metric("❌ Fallos", fallos)
            col3.metric("⚪ En blanco", blancos)
            if preguntas_con_respuesta > 0:
                col4.metric("💯 Nota", f"{(aciertos / preguntas_con_respuesta) * 10:.2f}/10")
                st.progress(aciertos / preguntas_con_respuesta, text=f"{aciertos}/{preguntas_con_respuesta} correctas")
            else:
                col4.metric("💯 Nota", "N/A", help="No hay respuestas correctas cargadas")

            claves_sin_plantilla = [n for n in numeros_examen if n not in plantilla_filtrada]
            if claves_sin_plantilla:
                nums_sin = sorted(
                    it["numero"] for it in preguntas
                    if (it.get("clave") or str(it["numero"])) in claves_sin_plantilla
                )
                st.info(f"ℹ️ Preguntas sin respuesta correcta cargada: {', '.join(str(n) for n in nums_sin)}")

            # ---- Estado de dominio guardado ----
            resultado_dominio = st.session_state.get("dominio_resultado")
            if resultado_dominio:
                if resultado_dominio.get("ok"):
                    rs = resultado_dominio["resumen"]
                    st.success(
                        f"🧭 Dominio actualizado en {rs['n']} pregunta(s): "
                        f"🟢 {rs['verde']} · 🔴 {rs['rojo']}"
                    )
                else:
                    st.warning(f"⚠️ No se pudo guardar tu progreso de dominio: {resultado_dominio.get('error')}")
                    if st.button("🔁 Reintentar guardado del progreso"):
                        ejecutar_guardado_dominio()
                        st.rerun()

            # ---- Resumen de preguntas falladas / en blanco ----
            st.markdown("---")
            st.subheader("🔍 Revisión de tus fallos")
            hay_fallos = False
            for it in preguntas:
                k = it.get("clave") or str(it["numero"])
                c = plantilla_filtrada.get(k)
                m = ur.get(k)
                if not c or m == c:
                    continue
                hay_fallos = True
                icono = "⚪" if m is None else "❌"
                etiqueta = f" (de {it['tabla_origen']})" if mostrar_origen and it.get("tabla_origen") else ""
                with st.expander(f"{icono} Pregunta {it['numero']}{etiqueta}: {it['pregunta'][:90]}{'…' if len(it['pregunta']) > 90 else ''}"):
                    st.markdown(f"**{it['pregunta']}**")
                    for opt in it["opciones"]:
                        letra = str(opt["letra"]).lower()
                        texto_opt = f"{letra.upper()}) {opt['texto']}"
                        if letra == c:
                            st.success(texto_opt, icon="✅")
                        elif letra == m:
                            st.error(texto_opt + "  ← tu respuesta", icon="❌")
                        else:
                            with st.container(border=True):
                                st.markdown(texto_opt)
                    if m is None:
                        st.warning(f"Sin contestar. La correcta era la {c.upper()}.")
            if not hay_fallos:
                st.success("🎉 ¡Ningún fallo ni pregunta en blanco!")

            # ---- Botones finales ----
            st.markdown("---")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🔄 Repetir estas preguntas", use_container_width=True):
                    reiniciar_intento()
                    st.rerun()
            with c2:
                if viene_de_supabase and not st.session_state.modo_test_errores:
                    if st.button("➕ Añadir errores a Test de errores", use_container_width=True):
                        fallos_por_tabla = {}
                        for it in preguntas:
                            if not it.get("tabla_origen"):
                                continue
                            k = it["clave"]
                            c = st.session_state.plantilla_respuestas.get(k)
                            if not c:
                                continue
                            m = ur.get(k)
                            # Solo FALLADAS (contestadas y distintas de la correcta), no las en blanco
                            if m is not None and m != c:
                                fallos_por_tabla.setdefault(it["tabla_origen"], []).append(it["numero"])

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

# --- PESTAÑA: PANEL DE PROGRESO ---
with tab_progreso:
    st.subheader("📈 Panel de progreso")
    if supabase_client is None:
        st.info("Configura SUPABASE_URL y SUPABASE_KEY en tu .env para ver tu progreso.")
    else:
        if st.button("🔄 Actualizar datos", key="actualizar_progreso"):
            _limpiar_caches_dominio()
            _pool_preguntas_cacheado.clear()
            st.rerun()

        pool_p, error_pool_p = obtener_pool_preguntas()
        estados_p, error_estados_p = obtener_estados_dominio()

        if error_pool_p:
            st.error(f"No se pudo leer el banco de preguntas: {error_pool_p}")
        elif error_estados_p:
            st.error(f"No se pudo leer 'dominio_preguntas': {error_estados_p}")
        elif not pool_p:
            st.info("No hay preguntas en las tablas configuradas.")
        else:
            conteo_tablas = {t: {"verde": 0, "rojo": 0, "nuevo": 0} for t in TABLAS_SUPABASE_DISPONIBLES}
            for ref in pool_p:
                info = estados_p.get(ref)
                estado = info["estado"] if info and info.get("estado") in ("verde", "rojo") else "nuevo"
                conteo_tablas[ref[0]][estado] += 1

            n_verde = sum(c["verde"] for c in conteo_tablas.values())
            n_rojo = sum(c["rojo"] for c in conteo_tablas.values())
            n_nuevo = sum(c["nuevo"] for c in conteo_tablas.values())
            n_total = len(pool_p)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("📚 Total", n_total)
            m2.metric("🟢 Verdes", n_verde)
            m3.metric("🔴 Rojas", n_rojo)
            m4.metric("⚪ Sin practicar", n_nuevo)
            st.progress(n_verde / n_total, text=f"{n_verde / n_total * 100:.1f}% del banco dominado (verde)")
            st.progress((n_total - n_nuevo) / n_total, text=f"{(n_total - n_nuevo) / n_total * 100:.1f}% del banco practicado")

            # ---- Desglose por tabla ----
            st.markdown("#### Por tabla / temario")
            df_tablas = pd.DataFrame(
                [
                    {
                        "Tabla": t,
                        "Verdes": c["verde"],
                        "Rojas": c["rojo"],
                        "Sin practicar": c["nuevo"],
                    }
                    for t, c in conteo_tablas.items()
                ]
            ).set_index("Tabla")
            df_tablas["Total"] = df_tablas.sum(axis=1)
            st.bar_chart(df_tablas[["Verdes", "Rojas", "Sin practicar"]], color=COLORES_ESTADO)
            st.dataframe(df_tablas, use_container_width=True)

            # ---- Evolución en el tiempo ----
            st.markdown("#### ¿Estoy mejorando?")
            historial_p, error_historial_p = obtener_historial()

            if historial_p:
                df_h = pd.DataFrame(historial_p)
                df_h["dt"] = pd.to_datetime(df_h["fecha"], utc=True, errors="coerce")
                df_h = df_h.dropna(subset=["dt"])
                df_h["dia"] = df_h["dt"].dt.tz_convert("Europe/Madrid").dt.strftime("%Y-%m-%d")

                ahora_p = pd.Timestamp.now(tz="UTC")
                ult7 = df_h[df_h["dt"] >= ahora_p - pd.Timedelta(days=7)]
                prev7 = df_h[(df_h["dt"] < ahora_p - pd.Timedelta(days=7)) & (df_h["dt"] >= ahora_p - pd.Timedelta(days=14))]
                if len(ult7) > 0:
                    pct_ult = (ult7["estado"] == "verde").mean() * 100
                    if len(prev7) > 0:
                        pct_prev = (prev7["estado"] == "verde").mean() * 100
                        st.metric("🟢 % de respuestas en verde (últimos 7 días)", f"{pct_ult:.1f}%",
                                  delta=f"{pct_ult - pct_prev:+.1f} pts vs. 7 días previos")
                    else:
                        st.metric("🟢 % de respuestas en verde (últimos 7 días)", f"{pct_ult:.1f}%")

                por_dia = df_h.groupby(["dia", "estado"]).size().unstack(fill_value=0)
                for col in ["verde", "rojo"]:
                    if col not in por_dia.columns:
                        por_dia[col] = 0
                por_dia = por_dia[["verde", "rojo"]].sort_index()

                porcentajes = por_dia.div(por_dia.sum(axis=1), axis=0) * 100
                porcentajes.columns = ["% verdes", "% rojas"]
                st.caption("Porcentaje de respuestas de cada color en los intentos de cada día")
                st.line_chart(porcentajes, color=COLORES_ESTADO[:2])

                por_dia.columns = ["Verdes", "Rojas"]
                st.caption("Preguntas practicadas por día")
                st.bar_chart(por_dia, color=COLORES_ESTADO[:2])
            else:
                if error_historial_p:
                    st.info(
                        "Para ver la evolución real intento a intento, crea la tabla opcional "
                        "`historial_dominio` (SQL en las instrucciones). Mientras tanto, esta gráfica usa la fecha "
                        "de la última práctica de cada pregunta."
                    )
                else:
                    st.info("Todavía no hay intentos en el historial. Haz algún test y vuelve aquí.")
                filas_ult = [
                    {"dia": (_parsear_fecha(i["ultima"]) or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d"),
                     "estado": i["estado"]}
                    for ref, i in estados_p.items() if ref in set(pool_p) and i.get("ultima")
                ]
                if filas_ult:
                    df_u = pd.DataFrame(filas_ult).groupby(["dia", "estado"]).size().unstack(fill_value=0)
                    for col in ["verde", "rojo"]:
                        if col not in df_u.columns:
                            df_u[col] = 0
                    df_u = df_u[["verde", "rojo"]].sort_index()
                    df_u.columns = ["Verdes", "Rojas"]
                    st.caption("Estado actual de las preguntas según el día de su última práctica")
                    st.bar_chart(df_u, color=COLORES_ESTADO[:2])

with tab_falladas:
    st.subheader("🔥 Preguntas más falladas")
    st.caption(
        "Ranking acumulado de todos tus tests: cuenta cada vez que una pregunta ha salido en rojo "
        f"(fallada o dejada en blanco). Solo aparecen las que llevan {MIN_FALLOS_MAS_FALLADAS} fallos o más, "
        f"y desaparecen cuando aciertas sus últimas {RACHA_ACIERTOS_PARA_QUITAR} veces seguidas. "
        "Despliega una pregunta para verla con sus respuestas."
    )
    if supabase_client is None:
        st.info("Configura SUPABASE_URL y SUPABASE_KEY en tu .env para ver tus preguntas más falladas.")
    else:
        if st.button("🔄 Actualizar ranking", key="actualizar_falladas"):
            _limpiar_caches_dominio()
            st.rerun()

        ranking_f, error_f = calcular_mas_falladas()
        if error_f:
            st.error(f"No se pudo leer 'historial_dominio': {error_f}")
        elif not ranking_f:
            st.info("🎉 Ninguna pregunta cumple todavía las condiciones del ranking.")
        else:
            n_ver_f = st.number_input(
                "¿Cuántas quieres ver?", min_value=1, max_value=500, value=50, step=10,
                key="n_ver_mas_falladas"
            )
            mostrar_f = ranking_f[:int(n_ver_f)]
            st.caption(f"**{len(ranking_f)}** pregunta(s) en el ranking · mostrando las {len(mostrar_f)} más falladas.")

            contenido_f, plantilla_f = _contenido_preguntas_cacheado(tuple((t, n) for t, n, _ in mostrar_f))
            por_clave_f = {p["clave"]: p for p in contenido_f}
            estados_f, _err_estados_f = obtener_estados_dominio()
            estados_f = estados_f or {}

            for pos, (tabla_f, num_f, veces_f) in enumerate(mostrar_f, start=1):
                it = por_clave_f.get(f"{tabla_f}__{num_f}")
                titulo_f = f"{pos}. [{tabla_f}] Pregunta {num_f} · fallada {veces_f} {'vez' if veces_f == 1 else 'veces'}"
                with st.expander(titulo_f):
                    if it is None:
                        st.warning("No se encontró el contenido de esta pregunta (quizá se borró de su tabla).")
                        continue
                    estado_actual = estados_f.get((tabla_f, num_f), {}).get("estado")
                    if estado_actual == "rojo":
                        st.caption("🔴 Ahora mismo está en rojo")
                    elif estado_actual == "verde":
                        st.caption("🟢 Ahora mismo está en verde")
                    c_f = plantilla_f.get(it["clave"])
                    st.markdown(f"**{it['pregunta']}**")
                    for opt in it["opciones"]:
                        letra = str(opt["letra"]).lower()
                        texto_opt = f"{letra.upper()}) {opt['texto']}"
                        if c_f and letra == c_f:
                            st.success(texto_opt, icon="✅")
                        else:
                            with st.container(border=True):
                                st.markdown(texto_opt)

with tab_marcadas:
    st.subheader("🔖 Tus preguntas marcadas")
    st.caption("Aquí puedes leer todas tus preguntas marcadas, repasarlas tranquilamente y quitar marcas.")
    if supabase_client is None:
        st.info("Configura SUPABASE_URL y SUPABASE_KEY en tu .env para usar el marcador.")
    else:
        if st.button("🔄 Actualizar lista de marcadas", key="actualizar_marcadas"):
            _marcadas_ref, _err_ref = obtener_preguntas_marcadas()
            if _marcadas_ref is not None:
                st.session_state.marcadas_set = _marcadas_ref
            else:
                st.error(_err_ref)
            st.rerun()

        if not st.session_state.marcadas_set:
            st.info("🔖 No tienes preguntas marcadas. Usa el icono de la esquina de cada pregunta en el cuestionario.")
        else:
            preguntas_marc_leer, plantilla_marc_leer, error_marc_leer = cargar_preguntas_marcadas()
            if error_marc_leer:
                st.error(error_marc_leer)
            elif not preguntas_marc_leer:
                st.info("🔖 No se encontró el contenido de las preguntas marcadas (quizá se borraron de sus tablas).")
            else:
                st.caption(f"Tienes **{len(preguntas_marc_leer)}** pregunta(s) marcada(s).")
                for it in preguntas_marc_leer:
                    ref = (it["tabla_origen"], it["numero"])
                    c = plantilla_marc_leer.get(it["clave"])
                    titulo = f"🔖 [{it['tabla_origen']}] Pregunta {it['numero']}: " \
                             f"{it['pregunta'][:85]}{'…' if len(it['pregunta']) > 85 else ''}"
                    with st.expander(titulo):
                        st.markdown(f"**{it['pregunta']}**")
                        for opt in it["opciones"]:
                            letra = str(opt["letra"]).lower()
                            texto_opt = f"{letra.upper()}) {opt['texto']}"
                            if c and letra == c:
                                st.success(texto_opt, icon="✅")
                            else:
                                with st.container(border=True):
                                    st.markdown(texto_opt)
                        if st.button("❌ Quitar marca", key=f"quitar_marca_{it['clave']}",
                                     use_container_width=True):
                            _, err_quitar = alternar_marca_pregunta(*ref)
                            if err_quitar:
                                st.error(err_quitar)
                            else:
                                st.session_state.marcadas_set.discard(ref)
                                st.toast(f"📑 Marca eliminada (pregunta {it['numero']}).")
                                st.rerun()

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