import { app, BrowserWindow, ipcMain, dialog, shell, Menu, type MenuItemConstructorOptions } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from 'node:child_process'
// electron-updater es CommonJS: desde ESM hay que importar el paquete por
// defecto y sacar autoUpdater de ahí (import con nombre falla en runtime).
import electronUpdater from 'electron-updater'

const { autoUpdater } = electronUpdater

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// The built directory structure
//
// ├─┬─┬ dist
// │ │ └── index.html
// │ │
// │ ├─┬ dist-electron
// │ │ ├── main.js
// │ │ └── preload.mjs
// │
process.env.APP_ROOT = path.join(__dirname, '..')

// 🚧 Use ['ENV_NAME'] avoid vite:define plugin - Vite@2.x
export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL ? path.join(process.env.APP_ROOT, 'public') : RENDERER_DIST

let win: BrowserWindow | null

// En desarrollo se ejecuta logic/cli.py con el intérprete de Python del
// sistema. En un build empaquetado (electron-builder) se usa en su lugar el
// ejecutable independiente generado por scripts/build-backend.cjs vía
// PyInstaller (modo --onedir: una carpeta, no un solo .exe, para no disparar
// heurísticas de antivirus), incluido como extraResource — así el usuario
// final no necesita tener Python instalado (sí necesita ffmpeg/ffprobe en el
// PATH).
const BACKEND_DIR = path.join(process.env.APP_ROOT, '..', 'logic')
const CLI_PATH = path.join(BACKEND_DIR, 'cli.py')
const BUNDLED_CLI_PATH = path.join(process.resourcesPath, 'logic', 'ffmpeg-cli-bridge', 'ffmpeg-cli-bridge.exe')

function resolvePythonExecutable(): string {
  const candidates = ['python', 'python3', 'py']
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['--version'])
    if (!probe.error) {
      return candidate
    }
  }
  return 'python'
}

let cachedPythonExecutable: string | null = null

function spawnCliProcess(): ChildProcessWithoutNullStreams {
  if (app.isPackaged) {
    return spawn(BUNDLED_CLI_PATH, [], { windowsHide: true })
  }
  cachedPythonExecutable ??= resolvePythonExecutable()
  return spawn(cachedPythonExecutable, ['-u', CLI_PATH], { cwd: BACKEND_DIR, windowsHide: true })
}

type OperationEntry = {
  child: ChildProcessWithoutNullStreams
  finished: boolean
}

const operations = new Map<string, OperationEntry>()

function sendOperationMessage(requestId: string, data: unknown) {
  win?.webContents.send('operation:message', { requestId, data })
}

function startOperation(operation: string, params: unknown): { requestId: string } {
  const requestId = randomUUID()
  const child = spawnCliProcess()

  operations.set(requestId, { child, finished: false })

  child.stdin.write(JSON.stringify({ operation, params }) + '\n')

  let buffer = ''
  child.stdout.on('data', (chunk: Buffer) => {
    buffer += chunk.toString()
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        const data = JSON.parse(trimmed)
        if (data.type === 'result') {
          const entry = operations.get(requestId)
          if (entry) entry.finished = true
        }
        sendOperationMessage(requestId, data)
      } catch {
        // Ignora líneas que no sean JSON válido (p. ej. logs sueltos de ffmpeg).
      }
    }
  })

  let stderrOutput = ''
  child.stderr.on('data', (chunk: Buffer) => {
    stderrOutput += chunk.toString()
  })

  child.on('close', () => {
    const entry = operations.get(requestId)
    if (entry && !entry.finished) {
      sendOperationMessage(requestId, {
        type: 'result',
        success: false,
        error: stderrOutput.trim() || 'El proceso de backend finalizó de forma inesperada.',
      })
    }
    operations.delete(requestId)
  })

  return { requestId }
}

function cancelOperation(requestId: string) {
  const entry = operations.get(requestId)
  if (entry && entry.child.stdin.writable) {
    entry.child.stdin.write(JSON.stringify({ cancel: true }) + '\n')
  }
}

function registerIpcHandlers() {
  ipcMain.handle('operation:start', (_event, { operation, params }) => {
    return startOperation(operation, params)
  })

  ipcMain.handle('operation:cancel', (_event, { requestId }: { requestId: string }) => {
    cancelOperation(requestId)
  })

  ipcMain.handle('dialog:openFile', async (_event, options: { filters?: Electron.FileFilter[]; multi?: boolean }) => {
    if (!win) return options?.multi ? [] : null
    const result = await dialog.showOpenDialog(win, {
      properties: options?.multi ? ['openFile', 'multiSelections'] : ['openFile'],
      filters: options?.filters,
    })
    if (result.canceled) return options?.multi ? [] : null
    return options?.multi ? result.filePaths : result.filePaths[0]
  })

  ipcMain.handle('dialog:openFolder', async () => {
    if (!win) return null
    const result = await dialog.showOpenDialog(win, { properties: ['openDirectory'] })
    if (result.canceled || result.filePaths.length === 0) return null
    return result.filePaths[0]
  })

  ipcMain.handle('shell:openPath', (_event, filePath: string) => {
    return shell.openPath(filePath)
  })

  ipcMain.handle('shell:openExternal', (_event, url: string) => {
    return shell.openExternal(url)
  })

  ipcMain.handle('dependencies:check', () => {
    return checkDependencies()
  })

  ipcMain.handle('update:getDownloaded', () => downloadedUpdateVersion)

  ipcMain.handle('update:install', () => {
    // isSilent=true: sin asistente de NSIS, solo reinstala y reabre la app.
    autoUpdater.quitAndInstall(true, true)
  })
}

// Actualizaciones automáticas desde GitHub Releases (configurado en
// electron-builder.json5 > publish). Solo en la app instalada: en desarrollo
// no hay app-update.yml y no tiene sentido. La descarga ocurre en segundo
// plano; si el usuario no pulsa "Reiniciar" en el aviso, la actualización se
// instala sola al cerrar la app (autoInstallOnAppQuit).
let downloadedUpdateVersion: string | null = null

function setupAutoUpdates() {
  if (!app.isPackaged) return

  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true

  autoUpdater.on('update-downloaded', (info) => {
    downloadedUpdateVersion = info.version
    win?.webContents.send('update:downloaded', info.version)
  })
  autoUpdater.on('error', (err) => {
    // Sin conexión, GitHub caído, etc.: no debe molestar al usuario, la app
    // sigue funcionando igual y lo reintentará en el próximo arranque.
    console.error('Error comprobando actualizaciones:', err)
  })

  autoUpdater.checkForUpdates().catch((err) => {
    console.error('Error comprobando actualizaciones:', err)
  })
}

function isOnPath(command: string): boolean {
  const probe = spawnSync(command, ['-version'], { windowsHide: true })
  return !probe.error
}

function checkDependencies(): { ffmpeg: boolean; ffprobe: boolean } {
  return { ffmpeg: isOnPath('ffmpeg'), ffprobe: isOnPath('ffprobe') }
}

function createAppMenu() {
  // Solo se conserva lo que un usuario podría necesitar de verdad (portapapeles
  // para pegar rutas/bitrates, zoom, y devtools para diagnosticar problemas):
  // el menú por defecto de Electron (File/View/Window/Help) no aporta nada aquí.
  const template: MenuItemConstructorOptions[] = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' as const }] : []),
    {
      // Se reconstruye a mano en vez de usar `role: 'editMenu'` porque ese
      // rol trae su propia etiqueta ("Edit") en inglés, que desentona con
      // el resto de la interfaz en español. Los roles de cada item se
      // mantienen para conservar el comportamiento y atajos nativos.
      label: 'Editar',
      submenu: [
        { role: 'undo' as const, label: 'Deshacer' },
        { role: 'redo' as const, label: 'Rehacer' },
        { type: 'separator' as const },
        { role: 'cut' as const, label: 'Cortar' },
        { role: 'copy' as const, label: 'Copiar' },
        { role: 'paste' as const, label: 'Pegar' },
        { role: 'selectAll' as const, label: 'Seleccionar todo' },
      ],
    },
    {
      label: 'Ver',
      submenu: [
        { role: 'reload' as const },
        { role: 'toggleDevTools' as const },
        { type: 'separator' as const },
        { role: 'resetZoom' as const },
        { role: 'zoomIn' as const },
        { role: 'zoomOut' as const },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function createWindow() {
  // La versión en el título permite comprobar de un vistazo si ya se aplicó
  // una actualización.
  win = new BrowserWindow({
    title: `FFmpeg GUI ${app.getVersion()}`,
    icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
    // Evita el flash de fondo blanco al arrancar mientras carga una app con
    // tema oscuro: sin esto, Electron pinta la ventana en blanco por defecto
    // hasta que el renderer termina de montar y aplicar sus estilos.
    backgroundColor: '#171717',
    // La columna de contenido tiene max-w-3xl (768px, fijo). 820 de ancho
    // deja solo ~18px de margen a cada lado (antes 900 dejaba ~58px, y 1200
    // casi 200px). 980 de alto es a propósito conservador: en monitores
    // normales (comprobado uno de 1680x1050) el área de trabajo real ronda
    // solo ~1000px una vez descontada la barra de tareas, así que pedir más
    // altura solo hace que Windows recorte la ventana igualmente. Con el
    // espaciado de Panel ya ajustado, incluso la página más alta (Imágenes
    // a video) solo necesita un scroll mínimo en vez de los ~200px de antes.
    width: 820,
    height: 980,
    minWidth: 700,
    minHeight: 600,
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
    },
  })

  // Sin esto el <title> de index.html sustituye al título con la versión.
  win.on('page-title-updated', (event) => event.preventDefault())

  if (VITE_DEV_SERVER_URL) {
    win.loadURL(VITE_DEV_SERVER_URL)
  } else {
    win.loadFile(path.join(RENDERER_DIST, 'index.html'))
  }
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
    win = null
  }
})

app.on('before-quit', () => {
  for (const entry of operations.values()) {
    entry.child.kill()
  }
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow()
  }
})

app.whenReady().then(() => {
  createAppMenu()
  registerIpcHandlers()
  createWindow()
  setupAutoUpdates()
})
