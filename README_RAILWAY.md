# AutoMonday Railway Deploy

Esta carpeta es una copia preparada para deploy. Los archivos originales de la carpeta padre no se modifican.

## 1. Crear repo en GitHub

Subir solo el contenido de esta carpeta `RAILWAY` al repo:

```text
index.html
servidor.py
FacturAuto_DriveSync.gs
Procfile
requirements.txt
railway.json
.env.example
.gitignore
README_RAILWAY.md
```

No subir `drive_config.json`, `processed_ids.json`, `pdfs_drive/`, `.env`, `.zip` ni `.rar`.

## 2. Crear proyecto en Railway

1. Entrar a Railway.
2. Crear un nuevo proyecto.
3. Elegir "Deploy from GitHub repo".
4. Seleccionar el repo donde subiste esta carpeta.

## 3. Configurar variables

En Railway, abrir el servicio y cargar estas variables:

```text
SHEET_ID=1R3MynU1TszAWLL5tnNv84Y7N8j45IkokzUaUHaXOhs8
GMAIL_SCRIPT_URL=https://script.google.com/a/macros/uala.com.ar/s/AKfycbzjwcpy5U1xblnRaSoiB22VStcqgoEeSQva3LnvtS67bUgtr4xTR3fiTrk7V7IuO3IC/exec
MONDAY_API_KEY=tu_token_de_monday
MONDAY_BOARD_ID=tu_board_id_de_monday
OPENAI_API_KEY=tu_token_de_openai
SYNC_INTERVAL=60
DATA_DIR=/data
```

Si usas Gemini o Anthropic, agregar también `GEMINI_API_KEY` o `ANTHROPIC_API_KEY`.

Verificar el `MONDAY_BOARD_ID` antes de deployar: el handoff menciona `196782965`, pero la config local original tenía otro ID.

## 4. Agregar volumen persistente

En Railway:

1. Abrir el servicio.
2. Ir a Volumes.
3. Crear un volumen.
4. Mount path: `/data`.

Esto conserva `processed_ids.json`, `drive_config.json` y los PDFs temporales entre reinicios.

## 5. Abrir la app

Cuando Railway termine el deploy, abrir la URL pública que genera. La app queda servida desde `/` y el backend desde la misma URL.

Ejemplos:

```text
https://tu-proyecto.up.railway.app/
https://tu-proyecto.up.railway.app/status
```

## Nota importante

Esta preparación toma la versión local actual del proyecto. El handoff menciona una versión v4 con procesamiento server-side por IA y `/sync-and-process`, pero esta carpeta contiene un servidor v3. Para deploy rápido, esta copia deja online la app actual con sync de Sheet, cola y upload a Monday.
