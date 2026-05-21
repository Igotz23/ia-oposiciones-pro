import streamlit as st
import json
import os
import base64
import re
from io import BytesIO
from pypdf import PdfReader
from pdf2image import convert_from_bytes
from groq import Groq
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

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
                        buffered = BytesIO()
                        img_rgb.save(buffered, format="JPEG", quality=80)
                        img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                        
                        prompt_resp = (
                            "Extrae ÚNICAMENTE las respuestas correctas de este documento. "
                            "Devuélvelo en formato JSON como un objeto donde la clave es el número de pregunta "
                            "y el valor es la letra de la respuesta correcta (a, b, c o d). "
                            "Ejemplo: {\"1\": \"a\", \"2\": \"c\", \"3\": \"b\"}. "
                            "NO incluyas explicaciones, solo el JSON puro."
                        )
                        
                        response = client.chat.completions.create(
                            model="meta-llama/llama-4-scout-17b-16e-instruct",
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
                # 1. Intentar leer texto digital primero
                file_bytes = uploaded_file.read()
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
                            buffered = BytesIO()
                            pagina_rgb.save(buffered, format="JPEG", quality=80)
                            img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                            
                            prompt_vision = (
                                "Transcribe de forma LITERAL todo el texto que veas en esta página de examen, "
                                "incluyendo los números de pregunta, enunciados y las opciones de respuesta (a, b, c, d). "
                                "No agregues comentarios propios ni introducciones."
                            )
                            
                            response_vision = client.chat.completions.create(
                                model="meta-llama/llama-4-scout-17b-16e-instruct",
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
                
                # 3. Generar cuestionario JSON estructurado
                with st.spinner("Estructurando cuestionario interactivo..."):
                    prompt_sistema = (
                        "Eres un extractor de datos de exámenes experto. Tu única tarea es identificar todas las preguntas "
                        "del texto proporcionado junto con sus opciones de respuesta (a, b, c, d).\n\n"
                        "Debes responder EXCLUSIVAMENTE con una estructura JSON válida que sea una lista de objetos. "
                        "Es crítico que NO incluyas introducciones, ni comentarios, ni saludos, ni formato markdown (no pongas ```json).\n\n"
                        "Formato exacto esperado:\n"
                        '[{"numero": 1, "pregunta": "Texto", "opciones": [{"letra": "a", "texto": "Opt A"}, {"letra": "b", "texto": "Opt B"}]}]'
                    )
                    
                    completion = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": prompt_sistema},
                            {"role": "user", "content": f"Aquí tienes el texto extraído del examen:\n\n{texto_completo}"}
                        ],
                        temperature=0
                    )
                    
                    respuesta_raw = completion.choices[0].message.content.strip()
                    
                    if "[" in respuesta_raw and "]" in respuesta_raw:
                        inicio = respuesta_raw.find("[")
                        final = respuesta_raw.rfind("]") + 1
                        if inicio != -1 and final != -1:
                            respuesta_raw = respuesta_raw[inicio:final]
                    
                    preguntas_parseadas = json.loads(respuesta_raw)
                    
                    if isinstance(preguntas_parseadas, list) and len(preguntas_parseadas) > 0:
                        st.session_state.preguntas_examen = preguntas_parseadas
                        st.session_state.respuestas_usuario = {}
                        st.session_state.corregido = False
                        st.success(f"✅ ¡Éxito! Detectadas {len(preguntas_parseadas)} preguntas.")
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
            
            opciones = [f"{opt['letra'].upper()}) {opt['texto']}" for opt in item["opciones"]]
            
            index_previo = None
            if num in st.session_state.respuestas_usuario:
                letra_g = st.session_state.respuestas_usuario[num]
                for i, opt in enumerate(item["opciones"]):
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
            preguntas_con_respuesta = len(st.session_state.plantilla_respuestas)
            
            # Contar aciertos solo sobre las preguntas que tienen respuesta en la plantilla
            aciertos = sum(
                1 for n in st.session_state.plantilla_respuestas.keys() 
                if st.session_state.respuestas_usuario.get(n) == st.session_state.plantilla_respuestas[n]
            )
            
            fallos = sum(
                1 for n in st.session_state.plantilla_respuestas.keys() 
                if st.session_state.respuestas_usuario.get(n) is not None 
                and st.session_state.respuestas_usuario.get(n) != st.session_state.plantilla_respuestas[n]
            )
            
            blancos = sum(
                1 for n in st.session_state.plantilla_respuestas.keys() 
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
            sin_plantilla = [n for n in [str(p["numero"]) for p in st.session_state.preguntas_examen] 
                           if n not in st.session_state.plantilla_respuestas]
            if sin_plantilla:
                st.info(f"ℹ️ Preguntas sin respuesta correcta cargada: {', '.join(sin_plantilla)}")

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
            
            prompt_chat = [
                {"role": "system", "content": f"Eres un tutor experto en oposiciones. Responde basándote estrictamente en este examen transcrito:\n\n{st.session_state.texto_examen}"}
            ]
            for chat in st.session_state.historial_chat:
                prompt_chat.append(chat)
                
            with st.chat_message("assistant"):
                with st.spinner("Pensando..."):
                    completion_chat = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=prompt_chat,
                        temperature=0.3
                    )
                    respuesta_chat = completion_chat.choices[0].message.content
                    st.markdown(respuesta_chat)
            
            st.session_state.historial_chat.append({"role": "assistant", "content": respuesta_chat})

with tab3:
    st.subheader("🔧 Diagnóstico y Depuración")
    
    with st.expander("📄 Texto del examen transcrito"):
        st.text_area("Contenido:", value=st.session_state.texto_examen, height=300)
    
    with st.expander("📄 Texto de respuestas correctas (raw)"):
        st.text_area("Contenido:", value=st.session_state.texto_respuestas, height=200)
    
    with st.expander("📋 Plantilla de respuestas parseada"):
        st.json(st.session_state.plantilla_respuestas)
    
    with st.expander("👤 Respuestas del usuario"):
        st.json(st.session_state.respuestas_usuario)
    
    if st.button("🗑️ Limpiar todo"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()