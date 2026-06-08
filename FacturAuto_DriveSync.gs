/**
 * FacturAuto — Google Apps Script v2
 * 
 * En lugar de enviar PDFs a localhost (imposible desde Google),
 * escribe los IDs de archivos nuevos en una Google Sheet pública.
 * El servidor Python lee esa Sheet y descarga los PDFs directamente.
 *
 * SETUP:
 *  1. Creá una Google Sheet en tu Gmail PERSONAL y copiá su ID
 *  2. Compartí esa Sheet como "cualquier persona con el link puede editar"
 *  3. Pegá el SHEET_ID abajo
 *  4. Ejecutá setupTrigger() una sola vez
 */

// ═══════════════════════════════════════════════
//  CONFIGURACIÓN
// ═══════════════════════════════════════════════

// ID de la carpeta de Drive de Ualá con los PDFs
const FOLDER_ID = '1D0pb8W8eqvDRuY-TIRzDJpHk2KPaTAQh';

// ID de la Google Sheet pública (de tu Gmail personal)
// URL: docs.google.com/spreadsheets/d/ESTE_ES_EL_ID/edit
const SHEET_ID = 'TU_SHEET_ID_ACA';

// Nombre de la hoja dentro del Sheet
const SHEET_NAME = 'cola';

// ═══════════════════════════════════════════════
//  LÓGICA PRINCIPAL
// ═══════════════════════════════════════════════

function sincronizarPDFs() {
  const props    = PropertiesService.getScriptProperties();
  const procesados = JSON.parse(props.getProperty('procesados') || '[]');

  try {
    const carpeta  = DriveApp.getFolderById(FOLDER_ID);
    const archivos = carpeta.getFilesByType(MimeType.PDF);

    const nuevos = [];
    while (archivos.hasNext()) {
      const f = archivos.next();
      if (!procesados.includes(f.getId())) {
        nuevos.push(f);
      }
    }

    if (nuevos.length === 0) {
      Logger.log('Sin PDFs nuevos.');
      return;
    }

    Logger.log(`${nuevos.length} PDF(s) nuevo(s) encontrado(s)`);

    // Abrir la Sheet y la hoja "cola"
    const ss    = SpreadsheetApp.openById(SHEET_ID);
    let   hoja  = ss.getSheetByName(SHEET_NAME);
    if (!hoja) {
      hoja = ss.insertSheet(SHEET_NAME);
      // Encabezados
      hoja.appendRow(['fileId', 'fileName', 'fechaRecibida', 'procesado']);
    }

    const fecha = Utilities.formatDate(
      new Date(), 'America/Argentina/Buenos_Aires', 'dd/MM/yyyy'
    );

    for (const archivo of nuevos) {
      hoja.appendRow([
        archivo.getId(),
        archivo.getName(),
        fecha,
        'pendiente'
      ]);
      procesados.push(archivo.getId());
      Logger.log(`✅ Encolado: ${archivo.getName()}`);
    }

    props.setProperty('procesados', JSON.stringify(procesados));
    Logger.log('Sheet actualizado OK');

  } catch (e) {
    Logger.log(`❌ Error: ${e.message}`);
  }
}

// ═══════════════════════════════════════════════
//  UTILIDADES
// ═══════════════════════════════════════════════

function setupTrigger() {
  // Borrar triggers anteriores
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'sincronizarPDFs')
    .forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger('sincronizarPDFs')
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log('✅ Trigger creado: sincronizarPDFs cada 5 minutos');
}

function detenerTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'sincronizarPDFs')
    .forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log('Trigger eliminado');
}

function resetearHistorial() {
  PropertiesService.getScriptProperties().deleteProperty('procesados');
  Logger.log('Historial reseteado');
}

function listarPDFs() {
  const props      = PropertiesService.getScriptProperties();
  const procesados = JSON.parse(props.getProperty('procesados') || '[]');
  const carpeta    = DriveApp.getFolderById(FOLDER_ID);
  const archivos   = carpeta.getFilesByType(MimeType.PDF);
  let total = 0, nuevos = 0;
  while (archivos.hasNext()) {
    const a = archivos.next();
    total++;
    const esNuevo = !procesados.includes(a.getId());
    if (esNuevo) nuevos++;
    Logger.log(`${esNuevo ? '🆕' : '✅'} ${a.getName()}`);
  }
  Logger.log(`Total: ${total} | Nuevos: ${nuevos}`);
}
