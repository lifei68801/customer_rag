import { NavLink, Outlet } from 'react-router-dom'

const subTabClass = ({ isActive }: { isActive: boolean }) =>
  `min-h-[44px] cursor-pointer rounded-control border border-subtle px-4 py-2 text-sm font-bold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
    isActive ? 'bg-accent-pink text-ink shadow-soft-sm' : 'bg-paper text-ink'
  }`

export function DataEntryPage() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">数据加工</h1>
      <div className="flex gap-2">
        <NavLink to="/admin/data-entry/manual" className={subTabClass}>
          实体列表
        </NavLink>
        <NavLink to="/admin/data-entry/etl" className={subTabClass}>
          表格导入
        </NavLink>
        <NavLink to="/admin/data-entry/review" className={subTabClass}>
          文档抽取
        </NavLink>
      </div>
      <Outlet />
    </div>
  )
}
