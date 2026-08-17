# 🎓 IA Oposiciones Pro - Sistema Inteligente de Exámenes

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Streamlit](https://img.shields.io/badge/streamlit-app-red) ![Groq](https://img.shields.io/badge/groq-API-orange) ![License](https://img.shields.io/badge/license-MIT-green)

**Convierte cualquier examen en PDF (digital o escaneado) en un cuestionario interactivo con corrección automática**

---

## 📖 ¿Por qué creé este proyecto?

Este proyecto nació de una necesidad personal: estoy preparando una oposición y necesitaba una forma fiable de convertir los exámenes en PDF (muchos de ellos escaneados) en tests interactivos con corrección automática.

**El problema:** Los test interactivos generados por Gemini (y otras IAs conversacionales) se inventaban las preguntas o modificaban el enunciado original. Esto es fatal cuando estudias para una oposición donde cada palabra cuenta.

**La solución:** Un sistema que lee literalmente el PDF de tu examen, mantiene el texto exacto de cada pregunta y opción, y te permite responderlo como si fuera un test online real, con corrección automática contra una plantilla de respuestas correctas.

**Ventajas clave:**

- ✅ **Texto fiel al original** - No se inventa ni modifica ninguna pregunta
- 📸 **Soporta PDFs escaneados** - Usa IA de Visión (OCR inteligente) para leer imágenes
- 🤖 **Chat con el examen** - Pregunta a la IA por qué una respuesta es correcta
- 📊 **Corrección automática** - Compara tus respuestas con una plantilla de respuestas correctas
- 🗄️ **Banco de preguntas en Supabase** - Guarda y reutiliza preguntas ya subidas, filtrando por rango y eligiendo el orden
- 🎯 **Ideal para oposiciones** - Diseñado pensando en exámenes tipo test de administración pública

---

## 🚀 ¿Cómo funciona?

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  PDF del Examen │────▶│  IA de Visión    │────▶│  Texto plano    │
│  (Digital/Scan) │     │  (Si es escaneado)│     │  del examen     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                              ┌───────────────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  IA Estructuradora│
                    │  (JSON estricto)  │
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ Cuestionario     │
                    │ Interactivo      │
                    │ (Streamlit)      │
                    └──────────────────┘
```

### Flujo de trabajo:

1. 📤 **Sube tu examen en PDF** (puede ser digital o escaneado/fotografiado)
2. 🧠 **La IA lo lee** - Si es escaneado, usa visión artificial para transcribir el texto literal
3. 📋 **Estructura automática** - Convierte el texto en un cuestionario JSON interactivo
4. ✍️ **Responde el test** - Interfaz tipo test con radio buttons
5. 📸 **Sube las respuestas correctas** - Imagen o PDF con la solución
6. 📊 **Corrige automáticamente** - Compara tus respuestas y muestra aciertos, fallos y nota

> ℹ️ Cada vez que procesas un PDF o lees un archivo de respuestas, la app llama a la IA en el momento: **no hay caché local en disco**, así que cada ejecución consulta a Groq de nuevo.

---

## 📦 Instalación

### 1. Clona el repositorio

```bash
git clone https://github.com/tu-usuario/ia-oposiciones-pro.git
cd ia-oposiciones-pro
```

### 2. Crea un entorno virtual (recomendado)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Instala las dependencias

```bash
pip install -r requirements.txt
```

### 4. Configura tus variables de entorno

Crea un archivo `.env` en la raíz del proyecto:

```
GROQ_API_KEY=tu_api_key_aqui

# Opcional: solo si quieres usar el banco de preguntas en Supabase
SUPABASE_URL=tu_url_de_supabase
SUPABASE_KEY=tu_key_de_supabase
```

🔑 Obtén tu API Key gratis en [console.groq.com](https://console.groq.com)

Supabase es **opcional**: si no configuras `SUPABASE_URL`/`SUPABASE_KEY`, la app funciona igual, simplemente no tendrás disponible la carga de bancos de preguntas guardados.

### 5. Instala Poppler (solo para PDFs escaneados)

**Windows:**
- Descarga Poppler desde [github.com/oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows)
- Extrae en `C:\poppler`
- El código ya apunta a `C:\poppler\bin`

**macOS:**
```bash
brew install poppler
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install poppler-utils
```

### 6. Ejecuta la aplicación

```bash
streamlit run app.py
```

La app se abrirá automáticamente en tu navegador en `http://localhost:8501`

---

## 📝 Cómo usar

### Modo 1: Examen digital (PDF con texto seleccionable)

1. Sube tu PDF en la barra lateral
2. Pulsa **🚀 Procesar Examen**
3. La app extrae el texto directamente del PDF
4. Responde las preguntas en la pestaña **📝 Cuestionario Interactivo**

### Modo 2: Examen escaneado o fotografiado

1. Sube tu PDF (o foto convertida a PDF)
2. Pulsa **🚀 Procesar Examen**
3. La app detecta que no hay texto y activa la IA de Visión
4. Cada página se convierte en imagen y se envía a la IA para transcribirla literalmente
5. Responde normalmente

### Cargar respuestas correctas

1. Sube una imagen o PDF con las respuestas correctas
2. Pulsa **🔍 Leer Respuestas Correctas**
3. La IA extrae el JSON de respuestas automáticamente
4. Ve a **📝 Cuestionario Interactivo** y pulsa **📊 Corregir Examen**

### Banco de preguntas en Supabase (opcional)

Si tienes Supabase configurado:

1. Elige la tabla/temario en el desplegable de la barra lateral
2. Filtra por número de pregunta (rangos tipo `200-300 450 460`, o vacío para todas)
3. Elige el orden: numérico, aleatorio, o aleatorio con opciones también desordenadas
4. Pulsa **☁️ Cargar preguntas guardadas de esta tabla**

> ⚠️ La app **solo lee** de Supabase desde este botón. No sube nada automáticamente al procesar un PDF; la subida a la base de datos, si la quieres, es una acción manual que tendrías que añadir aparte.

### Chat con el examen

En la pestaña **💬 Chat con el Examen** puedes preguntar cosas como:

- "¿Por qué la respuesta 3 es correcta?"
- "Explícame el tema de la pregunta 7"
- "Resume los conceptos del examen"

La IA responde estrictamente basándose en el texto del examen, no se inventa información.

---

## ⚙️ Configuración

### Variables de entorno

| Variable | Descripción | Obligatorio |
|---|---|---|
| `GROQ_API_KEY` | Tu API key de Groq | ✅ Sí |
| `SUPABASE_URL` | URL de tu proyecto de Supabase | ❌ Opcional (solo para banco de preguntas) |
| `SUPABASE_KEY` | API key de Supabase | ❌ Opcional (solo para banco de preguntas) |

### Modelos de IA utilizados

| Propósito | Modelo | Por qué |
|---|---|---|
| Visión (OCR) | `qwen/qwen3.6-27b` | Soporta análisis de imágenes, ideal para leer PDFs escaneados y respuestas en foto |
| Estructuración | `openai/gpt-oss-120b` | Gran capacidad para generar JSON estricto y seguir instrucciones precisas |
| Chat/Tutor | `openai/gpt-oss-120b` | Respuestas coherentes y basadas en contexto |

### Formato esperado del examen

La app funciona mejor con exámenes tipo test con este formato:

```
1. ¿En qué año se aprobó la Constitución Española?
a) 1975
b) 1978
c) 1981
d) 1983

2. El artículo 14 de la CE establece...
a) La igualdad ante la ley
b) El derecho de reunión
c) La libertad de empresa
d) El derecho a la educación
```

### Estructura de la tabla en Supabase (si usas el banco de preguntas)

Cada tabla listada en `TABLAS_SUPABASE_DISPONIBLES` debe tener, como mínimo:

| Columna | Tipo | Descripción |
|---|---|---|
| `numero` | `int4` | Número de la pregunta |
| `pregunta` | `text` | Enunciado |
| `opciones` | `jsonb` | Lista de opciones `[{"letra": "a", "texto": "..."}]` (también soporta el formato `{"a": "...", "b": "..."}`) |
| `respuesta` | `text` | Letra de la respuesta correcta (opcional, para corregir sin plantilla aparte) |

---

## 🛠️ Tecnologías utilizadas

- **[Streamlit](https://streamlit.io/)** - Framework web para Python, interfaz interactiva
- **[Groq API](https://groq.com/)** - Inferencia ultrarrápida de modelos LLM
- **[Supabase](https://supabase.com/)** - Banco de preguntas persistente (opcional)
- **PyPDF** - Extracción de texto de PDFs digitales
- **pdf2image** - Conversión de PDF a imágenes (requiere Poppler)
- **Pillow (PIL)** - Manipulación de imágenes y conversión de formatos
- **python-dotenv** - Gestión de variables de entorno

---

## 📂 Estructura del proyecto

```
ia-oposiciones-pro/
│
├── app.py                  # Aplicación principal (Streamlit)
├── requirements.txt        # Dependencias de Python
├── .env                    # Variables de entorno
├── .env.example             # Ejemplo de variables de entorno
├── .gitignore               # Archivos ignorados por Git
└── README.md                # Este archivo
```

---

## 🎯 Casos de uso ideales

- 🏛️ Oposiciones de administración pública (estado, autonómico, local)
- 📚 Preparación de exámenes tipo test de cualquier materia
- 📝 Repaso de exámenes antiguos convertidos a PDF
- 🎓 Universidad - Test de autoevaluación de asignaturas
- 🏢 Formación corporativa - Evaluaciones internas

---

## ⚠️ Limitaciones conocidas

- Los PDFs escaneados de muy baja calidad pueden dar errores de OCR
- El modelo de visión tiene un límite de tokens por imagen (páginas muy largas pueden cortarse)
- Requiere conexión a internet (usa API de Groq, y de Supabase si está configurado)
- Las respuestas correctas deben estar en formato claro para que la IA las interprete bien
- No hay caché local: cada vez que procesas un archivo se vuelve a llamar a la IA, aunque sea el mismo PDF que ya procesaste antes

---

## 🤝 Contribuir

¡Las contribuciones son bienvenidas! Si quieres mejorar el proyecto:

1. Haz fork del repositorio
2. Crea una rama (`git checkout -b feature/nueva-funcionalidad`)
3. Commit tus cambios (`git commit -m 'Añadir nueva funcionalidad'`)
4. Push a la rama (`git push origin feature/nueva-funcionalidad`)
5. Abre un Pull Request

### Ideas para futuras mejoras

- [ ] Soporte para múltiples formatos de respuesta (no solo a/b/c/d)
- [ ] Exportar resultados a Excel/PDF
- [ ] Modo examen con temporizador
- [ ] Estadísticas de progreso entre exámenes
- [ ] Soporte para preguntas de desarrollo (no solo test)
- [ ] Botón manual para subir preguntas procesadas a Supabase (actualmente solo se leen)

---

## 📄 Licencia

Este proyecto está bajo la licencia MIT. Puedes usarlo, modificarlo y distribuirlo libremente.

---

## 🙏 Agradecimientos

- A Groq por proporcionar inferencia ultrarrápida y gratuita (con límites) de modelos open-source
- A la comunidad de Streamlit por hacer tan fácil crear apps web con Python

---

⭐ Si te ha sido útil, dale una estrella al repo ⭐

*Hecho con ❤️ y mucho café para opositores de todo el mundo — empezando por mí mismo.*
