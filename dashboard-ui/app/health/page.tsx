'use client'
import { useDashboardContext } from '../../components/DashboardProvider'
import { Badge } from '../../components/Badge'
import { StatCard } from '../../components/StatCard'
import { age } from '../../lib/format'

export default function HealthPage() {
  const { health, data, loading } = useDashboardContext()

  if (loading && !health) return (
    <main className="max-w-[1200px] mx-auto px-4 py-8">
      <p className="font-mono text-sm" style={{ color: 'var(--text-muted)' }}>Loading…</p>
    </main>
  )

  const arm = health?.arm
  const executor = data?.executor

  return (
    <main className="max-w-[1200px] mx-auto px-4 md:px-6 py-8 space-y-8">
      <div>
        <p className="metric-label mb-1">KINETIC FLOW BY MOMENTUMX</p>
        <h1 className="text-2xl font-mono font-bold" style={{ color: 'var(--text-primary)' }}>
          System Health
        </h1>
      </div>

      {/* Arming status */}
      <section>
        <h2 className="section-header mb-4">Arming Status</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard
            label="Kill Switch"
            value={arm?.kill_switch_engaged ? 'ENGAGED' : 'CLEAR'}
            accentColor={arm?.kill_switch_engaged ? 'var(--negative)' : 'var(--positive)'}
            valueColor={arm?.kill_switch_engaged ? 'var(--negative)' : 'var(--positive)'}
          />
          <StatCard
            label="Live Trading"
            value={arm?.live_trading_enabled ? 'ENABLED' : 'DISABLED'}
            accentColor={arm?.live_trading_enabled ? 'var(--warning)' : 'var(--text-muted)'}
          />
          <StatCard
            label="Armed"
            value={arm?.armed ? 'ARMED' : 'SAFE'}
            accentColor={arm?.armed ? 'var(--negative)' : 'var(--positive)'}
            valueColor={arm?.armed ? 'var(--negative)' : 'var(--positive)'}
          />
          <StatCard
            label="Mode"
            value={health?.mode ?? '—'}
            sub={`${arm?.demo_strategies ?? 0} demo / ${arm?.live_strategies ?? 0} live`}
            accentColor="var(--border-focus)"
          />
        </div>
      </section>

      {/* Executor */}
      <section>
        <h2 className="section-header mb-4">Executor</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          <StatCard
            label="Status"
            value={executor?.ok ? 'OK' : executor?.error ? 'ERROR' : '—'}
            accentColor={executor?.ok ? 'var(--positive)' : 'var(--negative)'}
            valueColor={executor?.ok ? 'var(--positive)' : 'var(--negative)'}
          />
          <StatCard
            label="Last Updated"
            value={executor?.generated_at ? age(executor.generated_at) : '—'}
            accentColor="var(--border-dim)"
          />
          {executor?.error && (
            <StatCard
              label="Error"
              value="See below"
              sub={String(executor.error).slice(0, 60)}
              accentColor="var(--negative)"
            />
          )}
        </div>
      </section>

      {/* Service info */}
      <section>
        <h2 className="section-header mb-4">Service</h2>
        <div className="flex gap-2 flex-wrap">
          <Badge label={health?.service ?? 'unknown'} kind="neutral" />
          <Badge label={health?.mode ?? 'unknown'} kind="neutral" />
          {(health?.policies ?? []).map((p: string) => (
            <Badge key={p} label={p} kind="info" />
          ))}
        </div>
      </section>

      <a href="/" className="font-mono text-sm" style={{ color: 'var(--border-focus)' }}>
        ← Back to dashboard
      </a>
    </main>
  )
}
