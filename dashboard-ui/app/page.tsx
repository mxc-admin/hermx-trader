'use client'
import { ArmingBanner } from '../components/ArmingBanner'
import { TopBar } from '../components/TopBar'
import { SummaryCards } from '../components/SummaryCards'
import { StrategyGrid } from '../components/StrategyGrid'
import { ExecutionLedger } from '../components/ExecutionLedger'
import { StrategyAlertLog } from '../components/StrategyAlertLog'
import { OpenOrdersTable } from '../components/OpenOrdersTable'
import { ReconcileAlerts } from '../components/ReconcileAlerts'
import { OperatorAlerts } from '../components/OperatorAlerts'
import { useDashboardContext } from '../components/DashboardProvider'

export default function Home() {
  const { loading, error } = useDashboardContext()

  return (
    <>
      <ArmingBanner />
      <main className="max-w-[1200px] mx-auto px-4 md:px-6 py-4 pb-16 space-y-6">
        <TopBar />

        <SummaryCards />

        {/* Loading / error state */}
        {loading && !error && (
          <p className="font-mono text-sm" style={{ color: 'var(--text-muted)' }}>
            Loading…
          </p>
        )}
        {error && (
          <div className="rounded px-4 py-3 border font-mono text-sm"
            style={{ background: 'rgba(232,93,108,0.08)', borderColor: 'var(--negative)', color: 'var(--negative)' }}>
            {error}
          </div>
        )}

        {/* Strategy cards */}
        <StrategyGrid />

        {/* Tables */}
        <ExecutionLedger />
        <StrategyAlertLog />
        <OpenOrdersTable />
        <ReconcileAlerts />
        <OperatorAlerts />
      </main>
    </>
  )
}
