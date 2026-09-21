#!/usr/bin/env node
// Prueba de regresión end-to-end del puente logic/cli.py, usado por el
// frontend Electron. Genera sus propios ficheros de prueba con ffmpeg en un
// directorio temporal (nada se commitea al repo) y ejercita cada operación
// exactamente por el mismo mecanismo que electron/main.ts: spawn de
// `python logic/cli.py`, petición JSON por stdin, líneas JSON por stdout.
//
// Requiere: python (o python3/py) con acceso a logic/, y ffmpeg/ffprobe en
// el PATH — los mismos requisitos que la propia aplicación.
//
// Uso: node tests/test_cli_bridge.cjs

const { spawn, spawnSync, execFileSync } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const REPO_ROOT = path.join(__dirname, '..')
const LOGIC_DIR = path.join(REPO_ROOT, 'logic')
const CLI_PATH = path.join(LOGIC_DIR, 'cli.py')

function resolvePython() {
  const venvPython = path.join(REPO_ROOT, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  if (fs.existsSync(venvPython)) return venvPython
  for (const candidate of ['python', 'python3', 'py']) {
    const probe = spawnSync(candidate, ['--version'])
    if (!probe.error) return candidate
  }
  console.error('No se encontró un intérprete de Python en el PATH.')
  process.exit(1)
}

function requireOnPath(command) {
  const probe = spawnSync(command, ['-version'])
  if (probe.error) {
    console.error(`No se encontró "${command}" en el PATH. Instálalo antes de ejecutar estas pruebas.`)
    process.exit(1)
  }
}

function runOperation(python, operation, params, { cancelAfterMs } = {}) {
  return new Promise((resolve) => {
    const child = spawn(python, ['-u', CLI_PATH], { cwd: LOGIC_DIR, windowsHide: true })
    const events = []
    let buffer = ''
    let stderrOutput = ''
    let finished = false

    child.stdin.write(JSON.stringify({ operation, params }) + '\n')

    if (cancelAfterMs) {
      setTimeout(() => child.stdin.write(JSON.stringify({ cancel: true }) + '\n'), cancelAfterMs)
    }

    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString()
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try {
          const data = JSON.parse(trimmed)
          events.push(data)
          if (data.type === 'result') finished = true
        } catch {
          // línea no-JSON (p.ej. el comando ffmpeg impreso por depuración)
        }
      }
    })

    child.stderr.on('data', (chunk) => {
      stderrOutput += chunk.toString()
    })

    child.on('close', () => {
      if (!finished) {
        events.push({ type: 'result', success: false, error: stderrOutput.trim() || 'proceso cerrado sin resultado' })
      }
      resolve(events)
    })
  })
}

function probeDuration(file) {
  const output = execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file])
  return parseFloat(output.toString().trim())
}

function finalResult(events) {
  return events.find((e) => e.type === 'result')
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

async function test(name, fn) {
  process.stdout.write(`\n=== ${name} ===\n`)
  try {
    await fn()
    console.log(`PASS: ${name}`)
    return true
  } catch (err) {
    console.log(`FAIL: ${name} -> ${err.message}`)
    return false
  }
}

function makeFixtures(dir) {
  const p = (...parts) => path.join(dir, ...parts)
  const run = (args) => execFileSync('ffmpeg', args, { stdio: 'ignore' })

  fs.mkdirSync(p('imgseq'))
  fs.mkdirSync(p('folder1'))
  fs.mkdirSync(p('folder2'))

  run(['-y', '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264', '-c:a', 'aac', p('test.mp4')])
  run(['-y', '-f', 'lavfi', '-i', 'testsrc=duration=8:size=640x480:rate=30', '-c:v', 'libx264', '-preset', 'ultrafast', p('long.mp4')])
  run(['-y', '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=25', '-c:v', 'libx264', p('silent.mp4')])
  run(['-y', '-f', 'lavfi', '-i', 'sine=frequency=220:duration=2', p('audio.mp3')])
  // Mismo caso que los vídeos 1080x1920 de campaña: un clip con audio como primera pista
  // y vídeo como segunda (se unía detrás de silent.mp4 y el resultado salía recortado).
  run(['-y', '-f', 'lavfi', '-i', 'testsrc=duration=3:size=320x240:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-map', '1:a', '-map', '0:v', '-c:v', 'libx264', '-c:a', 'aac', p('audio_first.mp4')])
  for (const i of [1, 2, 3, 4, 5]) {
    run(['-y', '-f', 'lavfi', '-i', 'color=c=blue:size=64x64:d=1', '-frames:v', '1', p('imgseq', `seq_0${i}.png`)])
  }
  fs.copyFileSync(p('test.mp4'), p('folder1', 'clip_a.mp4'))
  fs.copyFileSync(p('test.mp4'), p('folder2', 'clip_a.mp4'))

  return p
}

async function main() {
  requireOnPath('ffmpeg')
  requireOnPath('ffprobe')
  const python = resolvePython()

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ffmpeg-gui-test-'))
  console.log(`Generando fixtures en ${workDir}...`)
  const p = makeFixtures(workDir)

  const results = []

  try {
    results.push(await test('img2vid', async () => {
      const events = await runOperation(python, 'img2vid', {
        folder: p('imgseq'), fps: '5', format: 'mp4 (H.264 8-bit)', crf: '23', fadeIn: 0, fadeOut: 0, prioritizeAudio: false,
      })
      const result = finalResult(events)
      assert(result?.success, `expected success, got ${JSON.stringify(result)}`)
      assert(events.some((e) => e.type === 'progress'), 'expected at least one progress event')
    }))

    results.push(await test('audio_edit add', async () => {
      const events = await runOperation(python, 'audio_edit', { mode: 'add', video: p('silent.mp4'), audio: p('audio.mp3'), format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('audio_edit remove', async () => {
      const events = await runOperation(python, 'audio_edit', { mode: 'remove', video: p('test.mp4'), format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('audio_edit replace', async () => {
      const events = await runOperation(python, 'audio_edit', { mode: 'replace', video: p('test.mp4'), audio: p('audio.mp3'), format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('cut_video (time mode)', async () => {
      const events = await runOperation(python, 'cut_video', { video: p('test.mp4'), cutMode: 'time', start: '0', duration: '1', fadeIn: 0, fadeOut: 0, format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('cut_video (frames mode)', async () => {
      const events = await runOperation(python, 'cut_video', { video: p('test.mp4'), cutMode: 'frames', start: '0', fps: '25', duration: '10', fadeIn: 0, fadeOut: 0, format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('limit_kbps', async () => {
      const events = await runOperation(python, 'limit_kbps', { video: p('test.mp4'), bitrate: '1M', maxrate: '1M', format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('scale_video', async () => {
      const events = await runOperation(python, 'scale_video', { video: p('test.mp4'), width: '160', height: '120', preset: 'ultrafast', crf: '30', format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('crop_video', async () => {
      const events = await runOperation(python, 'crop_video', { video: p('test.mp4'), top: 10, bottom: 10, left: 10, right: 10, fadeIn: 0, fadeOut: 0, format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('merge_videos (fast)', async () => {
      const events = await runOperation(python, 'merge_videos', { videos: [p('test.mp4'), p('test.mp4')], mode: 'fast', format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('merge_videos (compatible)', async () => {
      const events = await runOperation(python, 'merge_videos', { videos: [p('test.mp4'), p('test.mp4')], mode: 'compatible', preset: 'ultrafast', crf: '30', format: 'mp4' })
      assert(finalResult(events)?.success, 'expected success')
    }))

    results.push(await test('merge_videos (fast) with different tracks keeps full duration', async () => {
      const events = await runOperation(python, 'merge_videos', { videos: [p('silent.mp4'), p('audio_first.mp4')], mode: 'fast', preset: 'ultrafast', format: 'mp4' })
      const result = finalResult(events)
      assert(result?.success, `expected success, got ${JSON.stringify(result)}`)
      const duration = probeDuration(result.output)
      assert(Math.abs(duration - 5) < 0.2, `expected ~5s (2s + 3s), got ${duration}s`)
    }))

    results.push(await test('merge_auto (folder pairing)', async () => {
      const events = await runOperation(python, 'merge_auto', { folder1: p('folder1'), folder2: p('folder2'), mode: 'fast', format: 'mp4' })
      const result = finalResult(events)
      assert(result?.success, `expected overall success, got ${JSON.stringify(result)}`)
      assert(events.some((e) => e.type === 'pair_done' && e.success), 'expected at least one pair_done success')
    }))

    results.push(await test('cancellation mid-operation', async () => {
      const events = await runOperation(python, 'limit_kbps', { video: p('long.mp4'), bitrate: '500k', maxrate: '500k', format: 'mp4' }, { cancelAfterMs: 150 })
      assert(finalResult(events)?.cancelled === true, `expected cancelled result, got ${JSON.stringify(finalResult(events))}`)
    }))

    results.push(await test('unknown operation returns error, not crash', async () => {
      const events = await runOperation(python, 'does_not_exist', {})
      assert(finalResult(events)?.success === false, 'expected failure')
    }))
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true })
  }

  const passed = results.filter(Boolean).length
  console.log(`\n\n${passed}/${results.length} tests passed`)
  process.exit(passed === results.length ? 0 : 1)
}

main()
