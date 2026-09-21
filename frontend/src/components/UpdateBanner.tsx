import { useEffect, useState } from 'react'

// Aviso de actualización lista para instalar. La descarga la hace el proceso
// principal (electron-updater) en segundo plano; si el usuario ignora el aviso,
// la actualización se instala igualmente al cerrar la app.
export function UpdateBanner() {
  const [version, setVersion] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // La descarga puede haber terminado antes de montar este componente.
    window.api
      .getDownloadedUpdate()
      .then((downloaded) => {
        if (!cancelled && downloaded) setVersion(downloaded)
      })
      .catch(() => {})
    const unsubscribe = window.api.onUpdateDownloaded((downloaded) => setVersion(downloaded))
    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [])

  if (!version) return null

  return (
    <div className="w-full mb-4 p-3 rounded-xl bg-emerald-950 border border-emerald-800 text-emerald-200 text-sm flex flex-wrap items-center justify-between gap-2">
      <span>
        Hay una nueva versión (<strong>{version}</strong>) lista. Se instalará al cerrar la aplicación, o ahora mismo
        si reinicias (se cancelarán las tareas en curso).
      </span>
      <button
        type="button"
        onClick={() => window.api.installUpdate()}
        className="px-3 py-1 rounded-lg bg-emerald-500 text-black cursor-pointer hover:bg-emerald-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300"
      >
        Reiniciar y actualizar
      </button>
    </div>
  )
}
