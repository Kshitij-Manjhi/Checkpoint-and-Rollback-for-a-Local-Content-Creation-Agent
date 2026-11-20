/* Shared pure rendering helper for the recovery dashboard and Node tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DraftGuardRecoveryMetrics = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const n = value => Number.isFinite(Number(value)) ? Number(value) : null;
  const pct = value => { const x=n(value); return x===null?'Unavailable':`${(x*100).toFixed(1)}%`; };
  const fixed = (value, places=2) => { const x=n(value); return x===null?'Unavailable':x.toFixed(places); };
  function renderCards(report) {
    const metrics = report && report.metrics;
    if (!metrics) return '<p class="quality-empty">No results yet. Run a Recovery Experiment to populate these metrics.</p>';
    const recovery=metrics.recovery_success_rate||{}, latency=metrics.rollback_latency_ms||{},
      lost=metrics.lost_work_percentage||{}, falseRate=metrics.false_rollback_trigger_rate||{}, quality=metrics.post_recovery_quality||{};
    const cards=[
      ['Recovery success rate',pct(recovery.value),`${recovery.numerator??'—'} / ${recovery.denominator??'—'} eligible attempts`,recovery.definition],
      ['Rollback latency',latency.mean==null?'Unavailable':`${fixed(latency.mean)} ms`,
        `median ${latency.median==null?'—':fixed(latency.median)} ms · p95 ${latency.p95==null?'Unavailable':`${fixed(latency.p95)} ms`} · n=${latency.sample_count??0}`,
        latency.p95_unavailable_reason||latency.definition],
      ['Lost work',lost.value==null?'Unavailable':`${fixed(lost.value,1)}%`,
        `${lost.numerator??'—'} ${lost.work_unit||'units'} lost / ${lost.denominator??'—'} created`,lost.definition],
      ['False rollback triggers',pct(falseRate.value),
        `${falseRate.numerator??'—'} / ${falseRate.denominator??'—'} healthy controls`,falseRate.definition],
      ['Post-recovery quality',quality.value==null?'Unavailable':`${fixed(quality.value,1)} / 100`,
        `baseline ${quality.baseline_mean==null?'—':`${fixed(quality.baseline_mean,1)} / 100`} · Δ ${quality.delta_from_baseline==null?'—':fixed(quality.delta_from_baseline,1)}`,
        quality.definition],
    ];
    return `<div class="recovery-metric-grid">${cards.map(([title,value,detail,definition])=>
      `<article class="metric-card recovery-metric-card"><span>${escape(title)}</span><strong>${escape(value)}</strong><small>${escape(detail)}</small><small>${escape(definition||'No definition returned.')}</small></article>`).join('')}</div>`;
  }
  return {renderCards};
});
