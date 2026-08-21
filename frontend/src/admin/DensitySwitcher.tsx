import { useAdminDensity, type DensityId } from './DensityContext'

const DENSITY_OPTIONS: { id: DensityId; label: string }[] = [
  { id: 'standard', label: '标准' },
  { id: 'compact', label: '紧凑' },
]

export function DensitySwitcher() {
  const { density, setDensity } = useAdminDensity()

  return (
    <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
      列表密度
      <select
        value={density}
        onChange={(event) => setDensity(event.target.value as DensityId)}
        aria-label="切换列表密度"
        className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
      >
        {DENSITY_OPTIONS.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
