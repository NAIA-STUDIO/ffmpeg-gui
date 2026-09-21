import { useState } from 'react'
import Img2Vid from './pages/Img2Vid'
import EditAudio from './pages/EditAudio'
import CutVideo from './pages/CutVideo'
import LimitKps from './pages/LimitKps'
import ScaleVideo from './pages/ScaleVideo'
import TrimVideo from './pages/TrimVideo'
import MergeVideos from './pages/MergeVideos'
import { DependencyBanner } from './components/DependencyBanner'
import { UpdateBanner } from './components/UpdateBanner'

const PAGES = [
  { label: 'Imágenes a video', component: Img2Vid },
  { label: 'Editar audio', component: EditAudio },
  { label: 'Cortar Video', component: CutVideo },
  { label: 'Limitar Kps', component: LimitKps },
  { label: 'Escalar Video', component: ScaleVideo },
  { label: 'Recortar Video', component: TrimVideo },
  { label: 'Unir Videos', component: MergeVideos },
]

export default function Layout() {
  const [activeIndex, setActiveIndex] = useState(0)
  const ActivePage = PAGES[activeIndex].component

  return (
    <div className="max-w-3xl mx-auto p-6">
      <UpdateBanner />
      <DependencyBanner />

      <nav className="w-full mb-8 flex flex-row flex-wrap gap-1">
        {PAGES.map((page, index) => (
          <button
            key={page.label}
            className={`px-3 py-1 cursor-pointer rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 ${
              index === activeIndex ? 'bg-primary text-black' : 'bg-neutral-700 text-white hover:bg-neutral-600'
            }`}
            onClick={() => setActiveIndex(index)}
          >
            {page.label}
          </button>
        ))}
      </nav>

      <main>
        <ActivePage />
      </main>
    </div>
  )
}
