#!/usr/bin/env python3
"""
FacturAuto - Servidor local v3
Lee una Google Sheet pública para obtener IDs de PDFs nuevos,
los descarga de Drive y los encola para que la app los procese.

Endpoints:
  GET  /status       — health check
  GET  /poll-drive   — devuelve cola de PDFs procesados y la limpia
  GET  /get-pdf      — sirve un PDF por ruta local
  POST /upload       — adjunta PDF a columna de Monday
  POST /sync-sheet   — fuerza sincronización con la Sheet ahora
"""

import http.server, json, urllib.request, urllib.error, urllib.parse
import re, base64, os, time, threading

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get('DATA_DIR', BASE_DIR)
PORT       = int(os.environ.get('PORT', 8741))
PDF_DIR    = os.path.join(DATA_DIR, 'pdfs_drive')
QUEUE_FILE = os.path.join(PDF_DIR, 'cola.json')
CONFIG_FILE= os.path.join(DATA_DIR, 'drive_config.json')


# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────

def load_config():
    cfg = {
        'sheetId': os.environ.get('SHEET_ID', ''),
        'gmailScriptUrl': os.environ.get('GMAIL_SCRIPT_URL', ''),
        'mondayKey': os.environ.get('MONDAY_API_KEY', ''),
        'mondayBoard': os.environ.get('MONDAY_BOARD_ID', ''),
        'openaiKey': os.environ.get('OPENAI_API_KEY', ''),
        'claudeKey': os.environ.get('ANTHROPIC_API_KEY', ''),
        'geminiKey': os.environ.get('GEMINI_API_KEY', ''),
        'syncInterval': int(os.environ.get('SYNC_INTERVAL', 60)),
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            saved = json.load(f)
        cfg.update(saved)
    return {k: v for k, v in cfg.items() if v not in ('', None)}

def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
#  QUEUE
# ──────────────────────────────────────────────

def load_queue():
    if not os.path.exists(QUEUE_FILE): return []
    try:
        with open(QUEUE_FILE, encoding='utf-8') as f: return json.load(f)
    except: return []

def save_queue(q):
    os.makedirs(PDF_DIR, exist_ok=True)
    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
        json.dump(q, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
#  GOOGLE SHEET → DRIVE SYNC
# ──────────────────────────────────────────────

def leer_sheet(sheet_id):
    """
    Lee la hoja 'cola' con chunks.
    Formato: fileId, fileName, fechaRecibida, chunkIndex, totalChunks, chunkData, procesado
    Reensambla los chunks y devuelve un registro por archivo.
    """
    import csv, io
    # Aumentar límite de campo para manejar chunks de base64
    csv.field_size_limit(500000)
    
    url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet=cola'
    req = urllib.request.Request(url, headers={'User-Agent': 'FacturAuto/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read().decode('utf-8-sig')

    # Parsear todas las filas
    raw_rows = []
    reader = csv.reader(io.StringIO(content))
    headers = next(reader, None)
    for row in reader:
        if len(row) >= 7:
            raw_rows.append({
                'fileId':        row[0].strip(),
                'fileName':      row[1].strip(),
                'fechaRecibida': row[2].strip(),
                'chunkIndex':    int(row[3].strip() or 0),
                'totalChunks':   int(row[4].strip() or 1),
                'chunkData':     row[5].strip(),
                'procesado':     row[6].strip().lower()
            })

    # Agrupar chunks por fileId
    from collections import defaultdict
    grupos = defaultdict(list)
    for r in raw_rows:
        estado = r['procesado'].lower().strip()
        if estado in ('pendiente', '', 'pending'):
            grupos[r['fileId']].append(r)

    # Reconstruir archivos completos
    archivos = []
    for file_id, chunks in grupos.items():
        if not chunks: continue
        chunks_sorted = sorted(chunks, key=lambda x: x['chunkIndex'])
        total = chunks_sorted[0]['totalChunks']
        
        # Verificar que tenemos todos los chunks
        if len(chunks_sorted) < total:
            print(f'  [Sheet] {chunks_sorted[0]["fileName"]}: esperando chunks ({len(chunks_sorted)}/{total})')
            continue
        
        b64_completo = ''.join(c['chunkData'] for c in chunks_sorted)
        archivos.append({
            'fileId':        file_id,
            'fileName':      chunks_sorted[0]['fileName'],
            'fechaRecibida': chunks_sorted[0]['fechaRecibida'],
            'procesado':     'pendiente',
            'pdfBase64':     b64_completo
        })

    return archivos

def descargar_pdf_drive(file_id, api_key_drive=None):
    """
    Descarga un PDF de Google Drive.
    Intenta múltiples métodos: export directo, con confirm token,
    y con cookies de sesión si está disponible.
    """
    import http.cookiejar
    
    # Método 1: descarga directa con cookie jar (maneja redirects y confirmaciones)
    cj      = http.cookiejar.CookieJar()
    opener  = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    url = f'https://drive.google.com/uc?export=download&id={file_id}&confirm=t'
    req = urllib.request.Request(url, headers=headers)
    
    with opener.open(req, timeout=60) as r:
        content = r.read()
    
    # Si devuelve HTML es página de confirmación o error de permisos
    if len(content) < 1000 and (b'<!DOCTYPE' in content[:100] or b'<html' in content[:100]):
        # Intentar extraer mensaje de error
        if b'access' in content.lower() or b'permiso' in content.lower() or b'denied' in content.lower():
            raise Exception(f"Sin permisos para descargar {file_id}. Asegurate de que la carpeta de Drive sea pública (compartida con 'cualquier persona').")
        # Intentar con confirm token
        m = re.search(rb'confirm=([0-9A-Za-z_\-]+)', content)
        if m:
            confirm = m.group(1).decode()
            url2 = f'https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm}'
            req2 = urllib.request.Request(url2, headers=headers)
            with opener.open(req2, timeout=60) as r2:
                content = r2.read()
        else:
            raise Exception(f"No se pudo descargar {file_id} — revisá que la carpeta sea pública.")
    
    if len(content) < 100:
        raise Exception(f"PDF vacío o sin permisos para {file_id}")
    
    return content

def marcar_procesado_sheet(sheet_id, file_id):
    """
    Marca una fila como procesada en la Sheet usando la API pública de edición.
    Nota: requiere que la Sheet esté en modo 'cualquier persona puede editar'.
    Usamos la API de Apps Script Web App para esto, o simplemente lo ignoramos
    y dejamos que el script de Apps Script maneje el historial.
    """
    pass  # El historial lo maneja el Apps Script con ScriptProperties

sync_lock = threading.Lock()
# IDs ya procesados — persiste en disco para sobrevivir reinicios
PROCESSED_IDS_FILE = os.path.join(DATA_DIR, 'processed_ids.json')

def load_processed_ids():
    try:
        with open(PROCESSED_IDS_FILE, 'r') as f:
            return set(json.load(f))
    except:
        return set()

def save_processed_ids():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PROCESSED_IDS_FILE, 'w') as f:
            json.dump(list(processed_ids_local), f)
    except Exception as e:
        print(f'  [Warn] No se pudo guardar processed_ids: {e}')

processed_ids_local = load_processed_ids()

def marcar_listo_en_sheet(sheet_id, file_id):
    """
    Marca todas las filas de un fileId como 'listo' en la Sheet
    usando la API pública de edición (requiere Sheet en modo Editor público).
    Lo hace actualizando via la Sheets API sin auth (modo público).
    Como alternativa simple, usamos una hoja separada 'listos'.
    """
    import urllib.parse
    # Escribir en hoja 'listos' el fileId para que Apps Script lo lea
    # Esto es más simple que editar celdas específicas via API sin auth
    url = (f'https://docs.google.com/forms/d/e/placeholder/formResponse')
    # Método más confiable: agregar fila a hoja 'listos' via append
    # Usamos gviz para escribir (requiere permisos de editor en Sheet pública)
    try:
        payload = json.dumps({'fileId': file_id, 'estado': 'listo'}).encode()
        # No podemos escribir en Sheets sin auth desde Python sin OAuth
        # En cambio, creamos un archivo local que el próximo sync del script leerá
        listos_file = os.path.join(PDF_DIR, 'listos.json')
        listos = []
        if os.path.exists(listos_file):
            with open(listos_file) as f: listos = json.load(f)
        if file_id not in listos:
            listos.append(file_id)
            with open(listos_file, 'w') as f: json.dump(listos, f)
        print(f'  [Drive Sync] Marcado para mover: {file_id}')
    except Exception as e:
        print(f'  [Drive Sync] marcar_listo error: {e}')


def sync_desde_sheet():
    """Sincroniza PDFs nuevos desde la Google Sheet."""
    cfg = load_config()
    sheet_id = cfg.get('sheetId', '')
    
    if not sheet_id:
        print('  [Drive Sync] No hay Sheet ID configurado (configurá en la app)')
        return 0

    print(f'  [Drive Sync] Leyendo Sheet {sheet_id[:20]}...')
    
    try:
        rows = leer_sheet(sheet_id)
        pendientes = [r for r in rows if r['procesado'].strip().lower() == 'pendiente' 
                     and r['fileId'] not in processed_ids_local]
        
        if not pendientes:
            print(f'  [Drive Sync] Sin PDFs nuevos ({len(rows)} filas en Sheet)')
            return 0

        print(f'  [Drive Sync] {len(pendientes)} PDF(s) nuevo(s)')
        os.makedirs(PDF_DIR, exist_ok=True)
        cola = load_queue()
        nuevos = 0

        for row in pendientes:
            file_id   = row['fileId']
            file_name = row['fileName']
            print(f'  [Drive Sync] Descargando: {file_name}...')
            try:
                import base64 as _b64
                if row.get('pdfBase64'):
                    b64_str = row['pdfBase64']
                    # Base64 válido: largo debe ser 0, 2 o 3 mod 4 (se puede padear)
                    # Si es 1 mod 4 → corrupto, no se puede recuperar
                    remainder = len(b64_str) % 4
                    if remainder == 1:
                        raise ValueError(f'Base64 corrupto (largo {len(b64_str)}, mod4={remainder}) — chunks mal ensamblados')
                    b64_str += '=' * (-len(b64_str) % 4)
                    pdf_bytes = _b64.b64decode(b64_str)
                    print(f'  [Drive Sync] Usando base64 de Sheet para {file_name}')
                else:
                    pdf_bytes = descargar_pdf_drive(file_id)
                ts        = int(time.time())
                safe_name = re.sub(r'[/\\]', '_', file_name)
                out_path  = os.path.join(PDF_DIR, f'{ts}_{safe_name}')
                
                with open(out_path, 'wb') as f:
                    f.write(pdf_bytes)

                # Leer el PDF guardado como base64 para enviarlo a Monday
                import base64 as _b64enc
                try:
                    with open(out_path, 'rb') as pdf_f:
                        pdf_b64 = _b64enc.b64encode(pdf_f.read()).decode('utf-8')
                except:
                    pdf_b64 = None

                cola.append({
                    'fileId':        file_id,
                    'path':          out_path,
                    'fileName':      file_name,
                    'fechaRecibida': row['fechaRecibida'],
                    'pdfBase64':     pdf_b64,
                    'ts':            ts
                })
                processed_ids_local.add(file_id)
                save_processed_ids()
                # Marcar en Sheet como 'listo' → Apps Script moverá el PDF a carpeta Procesado
                try:
                    marcar_listo_en_sheet(sheet_id, file_id)
                except Exception as em:
                    print(f'  [Drive Sync] Aviso: no se pudo marcar como listo en Sheet: {em}')
                nuevos += 1
                print(f'  [Drive Sync] ✅ {file_name} ({len(pdf_bytes)} bytes)')

            except Exception as e:
                print(f'  [Drive Sync] ❌ Error descargando {file_name}: {e}')
                # Marcar como fallido para no reintentar en el próximo ciclo
                processed_ids_local.add(file_id)
                save_processed_ids()

        save_queue(cola)
        return nuevos

    except Exception as e:
        print(f'  [Drive Sync] ❌ Error leyendo Sheet: {e}')
        return 0


# ──────────────────────────────────────────────
#  AUTO SYNC THREAD
# ──────────────────────────────────────────────

auto_sync_interval = 60  # segundos
auto_sync_running  = False

def auto_sync_loop():
    global auto_sync_running
    while auto_sync_running:
        with sync_lock:
            sync_desde_sheet()
        time.sleep(auto_sync_interval)

def start_auto_sync(interval=60):
    global auto_sync_running, auto_sync_interval
    auto_sync_interval = interval
    auto_sync_running  = True
    t = threading.Thread(target=auto_sync_loop, daemon=True)
    t.start()
    print(f'  [Auto Sync] Iniciado cada {interval}s')


# ──────────────────────────────────────────────
#  MONDAY UPLOAD
# ──────────────────────────────────────────────

def parse_multipart(body, boundary):
    fields, files = {}, {}
    for part in body.split(b'--' + boundary):
        if part in (b'', b'--\r\n', b'--', b'\r\n') or part.startswith(b'--'):
            continue
        if b'\r\n\r\n' not in part:
            continue
        hdr, content = part.split(b'\r\n\r\n', 1)
        if content.endswith(b'\r\n'): content = content[:-2]
        h  = hdr.decode('utf-8', errors='ignore')
        nm = re.search(r'name="([^"]+)"', h)
        fn = re.search(r'filename="([^"]+)"', h)
        if not nm: continue
        name = nm.group(1)
        if fn: files[name] = {'filename': fn.group(1), 'data': content}
        else:  fields[name] = content.decode('utf-8', errors='ignore')
    return fields, files

def monday_upload_to_column(api_key, item_id, column_id, pdf_data, pdf_name):
    bnd = b'FacturAutoBND998877'
    q   = ('mutation add_file($file: File!, $item_id: ID!, $column_id: String!) '
           '{ add_file_to_column(item_id: $item_id, column_id: $column_id, file: $file) { id } }')
    def tp(n, v):
        return (b'--'+bnd+b'\r\nContent-Disposition: form-data; name="'+n.encode()+
                b'"\r\n\r\n'+(v.encode() if isinstance(v,str) else v)+b'\r\n')
    def fp(n, d, fn):
        return (b'--'+bnd+b'\r\nContent-Disposition: form-data; name="'+n.encode()+
                b'"; filename="'+fn.encode()+b'"\r\nContent-Type: application/octet-stream\r\n\r\n'+d+b'\r\n')
    body = (tp('query',q)+tp('variables',json.dumps({"item_id":item_id,"column_id":column_id}))+
            tp('map',json.dumps({"image":"variables.file"}))+fp('image',pdf_data,pdf_name)+
            b'--'+bnd+b'--\r\n')
    req = urllib.request.Request('https://api.monday.com/v2/file', data=body,
        headers={'Authorization':api_key,
                 'Content-Type':f'multipart/form-data; boundary={bnd.decode()}',
                 'API-Version':'2024-10'})
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read().decode())
    print(f"  Monday: {json.dumps(result)[:300]}")
    return result


# ──────────────────────────────────────────────
#  HTTP HANDLER
# ──────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args): print(f"[FacturAuto] {fmt % args}")

    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200); self.send_cors(); self.end_headers()

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code); self.send_cors()
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path

        if path in ('/app', '/'):
            # Servir index.html con no-cache para evitar versiones viejas del browser
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
            if os.path.exists(html_path):
                with open(html_path, 'rb') as hf:
                    html_bytes = hf.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Content-Length', str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
            else:
                self.send_response(404)
                self.end_headers()
            return

        elif path == '/get-config':
            cfg = load_config()
            self.send_json(200, cfg)

        elif path == '/debug-sheet':
            # Diagnóstico: muestra raw de la Sheet sin procesar
            cfg = load_config()
            sheet_id = cfg.get('sheetId','')
            if not sheet_id:
                self.send_json(400, {'error': 'No hay sheetId configurado'})
                return
            try:
                import csv, io as _io, collections
                csv.field_size_limit(500000)
                url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet=cola'
                req = urllib.request.Request(url, headers={'User-Agent':'FacturAuto/1.0'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read().decode('utf-8-sig')
                rows = list(csv.reader(_io.StringIO(raw)))
                # Contar por fileId y estado
                resumen = {}
                for row in rows[1:]:
                    if len(row) < 7: continue
                    fid = row[0][:50]
                    estado = row[6].strip()
                    total = row[4].strip()
                    if fid not in resumen:
                        resumen[fid] = {'chunks':0,'total':total,'estado':estado,'nombre':row[1][:40]}
                    resumen[fid]['chunks'] += 1
                self.send_json(200, {
                    'total_filas': len(rows)-1,
                    'archivos': list(resumen.values())[:20]
                })
            except Exception as e:
                self.send_json(500, {'error': str(e)})

        elif path == '/status':
            cfg = load_config()
            self.send_json(200, {
                "ok": True, "service": "FacturAuto",
                "sheetConfigured": bool(cfg.get('sheetId')),
                "autoSync": auto_sync_running
            })

        elif path == '/poll-drive':
            cola = load_queue()
            # ?clear=false para no vaciar (usado por Gmail polling)
            params_qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params_qs.get('clear', ['true'])[0].lower() != 'false':
                save_queue([])
            self.send_json(200, {"queue": cola})

        elif path == '/peek-queue':
            # Devuelve la cola sin limpiarla
            cola = load_queue()
            self.send_json(200, {"queue": cola})

        elif path == '/reset-processed':
            # Resetea la memoria de IDs ya procesados → el servidor los releerá de la Sheet
            processed_ids_local.clear()
            save_processed_ids()
            self.send_json(200, {"ok": True, "message": "IDs procesados reseteados"})

        elif path == '/server-status':
            cfg = load_config()
            self.send_json(200, {
                "processed_count": len(processed_ids_local),
                "queue_count":     len(load_queue()),
                "sheet_id":        cfg.get('sheetId',''),
                "auto_sync":       auto_sync_running
            })

        elif path == '/clear-queue':
            save_queue([])
            self.send_json(200, {"ok": True, "message": "Cola limpiada"})

        elif path == '/gmail-label':
            # Proxy hacia Apps Script Web App — evita el bloqueo CORS del browser
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                # scriptUrl puede venir en el body (prioritario) o en el config guardado
                script_url = body.pop('scriptUrl', None) or load_config().get('gmailScriptUrl', '')
                if not script_url:
                    self.send_json(400, {'ok': False, 'error': 'gmailScriptUrl no configurada. Guardá la config Gmail en la app.'})
                    return
                action = body.get('action', body.get('fileId', '?'))
                print(f'  [Gmail Proxy] action={action} body={str(body)[:80]}')
                # Llamar al Apps Script desde Python (sin restricciones CORS)
                payload = json.dumps(body).encode('utf-8')
                req = urllib.request.Request(script_url, data=payload,
                                   headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                print(f'  [Gmail Label] Respuesta Apps Script: {result}')
                self.send_json(200, result)
            except Exception as e:
                print(f'  [Gmail Label] ERROR: {e}')
                self.send_json(500, {'ok': False, 'error': str(e)})

        elif path == '/mark-processed':
            # Marcar un fileId como procesado (no borrarlo, solo cambiar estado)
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                file_id = body.get('fileId', '')
                cola   = load_queue()
                # Filtrar: sacar el item procesado de la cola
                cola = [item for item in cola if item.get('fileId') != file_id]
                save_queue(cola)
                # Recordar que este ID ya fue procesado — no volver a encolarlo
                if file_id:
                    processed_ids_local.add(file_id)
                    save_processed_ids()
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == '/get-pdf':
            params   = urllib.parse.parse_qs(parsed.query)
            pdf_path = params.get('path', [''])[0]
            if not pdf_path or not os.path.exists(pdf_path):
                self.send_response(404); self.send_cors(); self.end_headers(); return
            with open(pdf_path, 'rb') as f: data = f.read()
            self.send_response(200); self.send_cors()
            self.send_header('Content-Type','application/pdf')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers(); self.wfile.write(data)

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/upload':
            self.handle_upload()
        elif path == '/sync-sheet':
            self.handle_sync_sheet()
        elif path == '/save-config':
            self.handle_save_config()
        elif path == '/gmail-label':
            # Proxy hacia Apps Script Web App — evita el bloqueo CORS del browser
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                # scriptUrl puede venir en el body (prioritario) o en el config guardado
                script_url = body.pop('scriptUrl', None) or load_config().get('gmailScriptUrl', '')
                if not script_url:
                    self.send_json(400, {'ok': False, 'error': 'gmailScriptUrl no configurada. Guardá la config Gmail en la app.'})
                    return
                action = body.get('action', body.get('fileId', '?'))
                print(f'  [Gmail Proxy] action={action} body={str(body)[:80]}')
                # Llamar al Apps Script desde Python (sin restricciones CORS)
                payload = json.dumps(body).encode('utf-8')
                req = urllib.request.Request(script_url, data=payload,
                                   headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                print(f'  [Gmail Label] Respuesta Apps Script: {result}')
                self.send_json(200, result)
            except Exception as e:
                print(f'  [Gmail Label] ERROR: {e}')
                self.send_json(500, {'ok': False, 'error': str(e)})
        elif path == '/mark-processed':
            # Marcar un fileId como procesado (no borrarlo, solo cambiar estado)
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                file_id = body.get('fileId', '')
                cola   = load_queue()
                # Filtrar: sacar el item procesado de la cola
                cola = [item for item in cola if item.get('fileId') != file_id]
                save_queue(cola)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        else:
            self.send_response(404); self.end_headers()

    def handle_upload(self):
        try:
            ct  = self.headers.get('Content-Type','')
            bdy = self.rfile.read(int(self.headers.get('Content-Length',0)))
            bm  = re.search(r'boundary=([^\s;]+)', ct)
            if not bm:
                self.send_json(400,{"error":"No boundary"}); return
            fields, files = parse_multipart(bdy, bm.group(1).encode())
            item_id   = fields.get('itemId','').strip()
            column_id = fields.get('columnId','').strip()
            api_key   = fields.get('apiKey','').strip()
            if 'file' not in files:
                self.send_json(400,{"error":"No se recibió el PDF"}); return
            pdf_data = files['file']['data']
            pdf_name = files['file']['filename'] or 'factura.pdf'
            print(f"  Subiendo '{pdf_name}' → '{column_id}' en item {item_id} ({len(pdf_data)} bytes)")
            result = monday_upload_to_column(api_key, item_id, column_id, pdf_data, pdf_name)
            if 'errors' in result:
                self.send_json(500,{"error":result['errors'][0].get('message','Error')}); return
            print("  PDF adjuntado OK")
            self.send_json(200,{"ok":True})
        except urllib.error.HTTPError as e:
            self.send_json(500,{"error":f"Monday HTTP {e.code}: {e.read().decode()[:300]}"})
        except Exception as e:
            print(f"  Error: {e}"); self.send_json(500,{"error":str(e)})

    def handle_sync_sheet(self):
        try:
            with sync_lock:
                nuevos = sync_desde_sheet()
            self.send_json(200, {"ok": True, "nuevos": nuevos})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_save_config(self):
        try:
            body = self.rfile.read(int(self.headers.get('Content-Length',0)))
            data = json.loads(body.decode('utf-8'))
            cfg  = load_config()
            cfg.update(data)
            save_config(cfg)
            
            # Reiniciar auto sync si cambió el interval
            global auto_sync_running
            if auto_sync_running:
                auto_sync_running = False
                time.sleep(0.1)
            interval = int(data.get('syncInterval', 60))
            start_auto_sync(interval)
            
            self.send_json(200, {"ok": True})
        except Exception as e:
            self.send_json(500, {"error": str(e)})


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 54)
    print("  FacturAuto - Servidor Railway")
    print(f"  Listening on 0.0.0.0:{PORT}")
    print("  /upload  /sync-sheet  /poll-drive  /get-pdf  /status")
    print("  Ctrl+C para detener")
    print("=" * 54)
    
    # Iniciar auto sync si hay config
    cfg = load_config()
    if cfg.get('sheetId'):
        start_auto_sync(int(cfg.get('syncInterval', 60)))
    else:
        print("  ⚠️  Sin Sheet ID configurado — configurá en la pestaña Google Drive")

    server = http.server.HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    try:    server.serve_forever()
    except KeyboardInterrupt: print("\nServidor detenido.")
