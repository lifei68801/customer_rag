import { useAdminSkin, type SkinId } from './SkinContext'

const SKIN_OPTIONS: { id: SkinId; label: string }[] = [
  { id: 'default', label: '默认' },
  { id: 'dark', label: '暗色' },
  { id: 'business-blue', label: '商务蓝' },
]

export function SkinSwitcher() {
  const { skin, setSkin } = useAdminSkin()

  return (
    <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
      配色皮肤
      <select
        value={skin}
        onChange={(event) => setSkin(event.target.value as SkinId)}
        aria-label="切换配色皮肤"
        className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
      >
        {SKIN_OPTIONS.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
