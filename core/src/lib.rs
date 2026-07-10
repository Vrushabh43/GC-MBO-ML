//! gc_core — compiled performance core for the GC order-flow system.
//!
//! Phase 0 Option A: Rust + PyO3. One code path for historical and live:
//! the same `Engine::process` handles records from the bundled DBN file
//! decoder (historical replay) and records pushed one-by-one from Python
//! (synthetic tests today, live feed later).

mod book;
mod engine;
mod flow;
mod lifecycle;
mod types;

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use dbn::decode::DecodeRecordRef;

use engine::Engine;
use flow::FlowConfig;
use lifecycle::LifecycleConfig;
use types::{SIDE_ASK, SIDE_BID};

fn side_byte(side: char) -> PyResult<u8> {
    match side {
        'B' => Ok(SIDE_BID),
        'A' => Ok(SIDE_ASK),
        _ => Err(PyValueError::new_err("side must be 'B' or 'A'")),
    }
}

/// The MBO engine: order store + price-level book + invariants.
#[pyclass]
pub struct MboEngine {
    inner: Engine,
}

#[pymethods]
impl MboEngine {
    /// max_incidents: detailed-incident ring size (counters always exact).
    /// audit_interval: records between full store-vs-levels audits (R1).
    /// halt_on_engine_invariant: halt processing on store/levels mismatch.
    /// lifecycle: enable the Phase 2 order-lifecycle/queue tracker.
    /// iceberg_window_ns / iceberg_clip_tol: refill-chain heuristic bounds.
    #[new]
    #[pyo3(signature = (max_incidents=10000, audit_interval=1_000_000, halt_on_engine_invariant=true,
                        lifecycle=false, iceberg_window_ns=2_000_000, iceberg_clip_tol=1.0))]
    fn new(
        max_incidents: usize,
        audit_interval: u64,
        halt_on_engine_invariant: bool,
        lifecycle: bool,
        iceberg_window_ns: u64,
        iceberg_clip_tol: f64,
    ) -> Self {
        let lc = lifecycle.then_some(LifecycleConfig {
            iceberg_window_ns,
            iceberg_clip_tol,
        });
        MboEngine {
            inner: Engine::new(max_incidents, audit_interval, halt_on_engine_invariant, lc),
        }
    }

    /// Process a single MBO record (synthetic tests / live push path).
    /// action and side are single characters, e.g. 'A' and 'B'.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (ts_event, action, side, price, size, order_id,
                        flags=0, channel_id=0, sequence=0, instrument_id=1))]
    fn push(
        &mut self,
        ts_event: u64,
        action: char,
        side: char,
        price: i64,
        size: u32,
        order_id: u64,
        flags: u8,
        channel_id: u8,
        sequence: u32,
        instrument_id: u32,
    ) -> bool {
        self.inner.process(
            ts_event,
            price,
            size,
            order_id,
            flags,
            channel_id,
            action as u8,
            side as u8,
            sequence,
            instrument_id,
        )
    }

    /// Flush the open matching-event group (end of stream / end of test).
    fn finish(&mut self) {
        self.inner.finish();
    }

    /// Replay a .dbn.zst file through the engine (GIL released).
    /// Returns (mbo_records_processed, seconds, non_mbo_records_skipped).
    fn replay_file(&mut self, py: Python<'_>, path: &str) -> PyResult<(u64, f64, u64)> {
        let inner = &mut self.inner;
        let path = path.to_owned();
        py.detach(move || {
            let mut dec = dbn::decode::dbn::Decoder::from_zstd_file(&path)
                .map_err(|e| PyIOError::new_err(format!("open {}: {}", path, e)))?;
            let t0 = std::time::Instant::now();
            let mut n: u64 = 0;
            let mut skipped: u64 = 0;
            loop {
                match dec.decode_record_ref() {
                    Ok(Some(rec)) => {
                        if let Some(m) = rec.get::<dbn::MboMsg>() {
                            inner.process(
                                m.hd.ts_event,
                                m.price,
                                m.size,
                                m.order_id,
                                m.flags.raw(),
                                m.channel_id,
                                m.action as u8,
                                m.side as u8,
                                m.sequence,
                                m.hd.instrument_id,
                            );
                            n += 1;
                        } else {
                            skipped += 1;
                        }
                    }
                    Ok(None) => break,
                    Err(e) => {
                        return Err(PyIOError::new_err(format!("decode error: {}", e)));
                    }
                }
            }
            inner.finish();
            Ok((n, t0.elapsed().as_secs_f64(), skipped))
        })
    }

    /// Engine counters as (name, value) pairs.
    fn stats(&self) -> Vec<(String, u64)> {
        self.inner
            .stats_pairs()
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect()
    }

    /// Detailed incidents: (kind, ts_event, instrument_id, order_id, sequence, detail).
    #[pyo3(signature = (limit=100))]
    fn incidents(&self, limit: usize) -> Vec<(String, u64, u32, u64, u32, String)> {
        self.inner
            .incidents()
            .iter()
            .take(limit)
            .map(|i| {
                (
                    i.kind.name().to_string(),
                    i.ts_event,
                    i.instrument_id,
                    i.order_id,
                    i.sequence,
                    i.detail.clone(),
                )
            })
            .collect()
    }

    /// Deterministic digest of full engine state (replay-twice CI test).
    fn state_digest(&self) -> u64 {
        self.inner.state_digest()
    }

    /// Instruments seen: (instrument_id, records, t_volume), sorted by id.
    fn instruments(&self) -> Vec<(u32, u64, u64)> {
        self.inner
            .records_per_instrument
            .iter()
            .map(|(iid, n)| {
                (
                    *iid,
                    *n,
                    self.inner.t_volume_per_instrument.get(iid).copied().unwrap_or(0),
                )
            })
            .collect()
    }

    /// Top-n levels, best first: [(price, total_size, order_count)].
    /// from_orders=True recomputes from the individual-order store
    /// (Milestone 1 requires both views).
    #[pyo3(signature = (instrument_id, side, n=10, from_orders=false))]
    fn top_levels(
        &self,
        instrument_id: u32,
        side: char,
        n: usize,
        from_orders: bool,
    ) -> PyResult<Vec<(i64, u64, u32)>> {
        let s = side_byte(side)?;
        Ok(self
            .inner
            .books
            .get(&instrument_id)
            .map(|b| {
                if from_orders {
                    b.top_levels_from_orders(s, n)
                } else {
                    b.top_levels(s, n)
                }
            })
            .unwrap_or_default())
    }

    /// ((bid_px, bid_size, bid_orders), (ask_px, ask_size, ask_orders)) or None.
    fn best_bid_ask(
        &self,
        instrument_id: u32,
    ) -> Option<((i64, u64, u32), (i64, u64, u32))> {
        let b = self.inner.books.get(&instrument_id)?;
        let (bp, bl) = b.best_bid()?;
        let (ap, al) = b.best_ask()?;
        Some((
            (bp, bl.total_size, bl.order_count),
            (ap, al.total_size, al.order_count),
        ))
    }

    /// Resting order lookup:
    /// (side, price, current_size, initial_size, filled_size, ts_added,
    ///  ts_last_updated, state, from_snapshot) or None.
    fn order(
        &self,
        instrument_id: u32,
        order_id: u64,
    ) -> Option<(String, i64, u32, u32, u32, u64, u64, u8, bool)> {
        let o = self.inner.books.get(&instrument_id)?.orders.get(&order_id)?;
        Some((
            (o.side as char).to_string(),
            o.price,
            o.current_size,
            o.initial_size,
            o.filled_size,
            o.ts_added,
            o.ts_last_updated,
            o.state as u8,
            o.from_snapshot,
        ))
    }

    /// FIFO queue position of an order at its price level (0 = front).
    fn queue_position(&self, instrument_id: u32, order_id: u64) -> Option<usize> {
        let b = self.inner.books.get(&instrument_id)?;
        let o = b.orders.get(&order_id)?;
        let side = if o.side == SIDE_BID { &b.bids } else { &b.asks };
        side.levels
            .get(&o.price)?
            .fifo
            .iter()
            .position(|&id| id == order_id)
    }

    /// Number of resting orders for an instrument.
    fn order_count(&self, instrument_id: u32) -> usize {
        self.inner
            .books
            .get(&instrument_id)
            .map(|b| b.orders.len())
            .unwrap_or(0)
    }

    /// Full cross-view consistency check; None if consistent, else the
    /// first mismatch description (R1 invariant).
    fn views_consistent(&self, instrument_id: u32) -> Option<String> {
        self.inner
            .books
            .get(&instrument_id)
            .and_then(|b| b.views_consistent().err())
    }

    /// Halt reason if an engine invariant tripped with halt policy on.
    fn halted(&self) -> Option<String> {
        self.inner.halted.clone()
    }

    // -- Phase 2: order lifecycle + queue engine ---------------------------

    /// Volume resting ahead of an order in its level's FIFO.
    fn volume_ahead(&self, instrument_id: u32, order_id: u64) -> Option<u64> {
        self.inner.books.get(&instrument_id)?.volume_ahead(order_id)
    }

    /// Liquidity-age distribution of a price level, front of queue first:
    /// [(order_id, age_ns, current_size, from_snapshot)].
    #[pyo3(signature = (instrument_id, side, price, ts_now))]
    fn level_ages(
        &self,
        instrument_id: u32,
        side: char,
        price: i64,
        ts_now: u64,
    ) -> PyResult<Vec<(u64, u64, u32, bool)>> {
        let s = side_byte(side)?;
        Ok(self
            .inner
            .books
            .get(&instrument_id)
            .map(|b| b.level_ages(s, price, ts_now))
            .unwrap_or_default())
    }

    /// Completed lifecycle records not yet drained.
    fn lifecycle_len(&self) -> usize {
        self.inner
            .lifecycle
            .as_ref()
            .map(|t| t.records.len())
            .unwrap_or(0)
    }

    /// Running deterministic digest over every emitted lifecycle record
    /// (replay-twice CI test for Phase 2 output).
    fn lifecycle_digest(&self) -> Option<u64> {
        self.inner.lifecycle.as_ref().map(|t| t.lifecycle_digest())
    }

    /// (records_emitted_total, iceberg_links_made, refill_slots_opened).
    fn lifecycle_stats(&self) -> Option<(u64, u64, u64)> {
        self.inner
            .lifecycle
            .as_ref()
            .map(|t| (t.emitted, t.links_made, t.refill_slots))
    }

    // -- Phase 3: per-group flow primitives --------------------------------

    /// Enable flow-primitive recording for one instrument (requires
    /// lifecycle=True). tick = one tick in fixed-point price units;
    /// near_ticks = "near the touch" band; levels = book depth tracked.
    #[pyo3(signature = (instrument_id, tick, near_ticks=5, levels=10))]
    fn enable_flow(
        &mut self,
        instrument_id: u32,
        tick: i64,
        near_ticks: i64,
        levels: usize,
    ) -> PyResult<()> {
        if !self.inner.enable_flow(FlowConfig {
            instrument_id,
            tick,
            near_ticks,
            levels,
        }) {
            return Err(PyValueError::new_err(
                "flow recording requires lifecycle=True (termination/refill primitives)",
            ));
        }
        Ok(())
    }

    /// Rows currently buffered / groups emitted in total.
    fn flow_stats(&self) -> Option<(usize, u64)> {
        self.inner
            .flow
            .as_ref()
            .map(|f| (f.cols.len(), f.groups_emitted))
    }

    /// Drain buffered flow-primitive rows as raw little-endian column
    /// buffers: [(column_name, numpy_dtype, bytes)]. The recorder keeps
    /// running; call after finish() for a full session, or periodically.
    fn flow_drain(&mut self, py: Python<'_>) -> PyResult<Vec<(String, String, Py<PyBytes>)>> {
        let Some(fr) = self.inner.flow.as_mut() else {
            return Err(PyValueError::new_err(
                "flow recording is not enabled (call enable_flow first)",
            ));
        };
        let levels = fr.cfg.levels;
        let r = fr.drain();

        fn b_u8(py: Python<'_>, v: &[u8]) -> Py<PyBytes> {
            PyBytes::new(py, v).unbind()
        }
        macro_rules! b_le {
            ($py:expr, $v:expr, $w:expr) => {{
                let mut buf = Vec::with_capacity($v.len() * $w);
                for x in $v.iter() {
                    buf.extend_from_slice(&x.to_le_bytes());
                }
                PyBytes::new($py, &buf).unbind()
            }};
        }
        let mut out: Vec<(String, String, Py<PyBytes>)> = vec![
            ("ts".into(), "<u8".into(), b_le!(py, r.ts, 8)),
            ("valid".into(), "u1".into(), b_u8(py, &r.valid)),
            ("bid_px".into(), "<i8".into(), b_le!(py, r.bid_px, 8)),
            ("ask_px".into(), "<i8".into(), b_le!(py, r.ask_px, 8)),
            ("bid_sz".into(), "<u4".into(), b_le!(py, r.bid_sz, 4)),
            ("ask_sz".into(), "<u4".into(), b_le!(py, r.ask_sz, 4)),
            ("bid_ct".into(), "<u4".into(), b_le!(py, r.bid_ct, 4)),
            ("ask_ct".into(), "<u4".into(), b_le!(py, r.ask_ct, 4)),
            ("depth_b_near".into(), "<u4".into(), b_le!(py, r.depth_b_near, 4)),
            ("depth_b_mid".into(), "<u4".into(), b_le!(py, r.depth_b_mid, 4)),
            ("depth_b_deep".into(), "<u4".into(), b_le!(py, r.depth_b_deep, 4)),
            ("depth_a_near".into(), "<u4".into(), b_le!(py, r.depth_a_near, 4)),
            ("depth_a_mid".into(), "<u4".into(), b_le!(py, r.depth_a_mid, 4)),
            ("depth_a_deep".into(), "<u4".into(), b_le!(py, r.depth_a_deep, 4)),
        ];
        for l in 0..levels {
            out.push((
                format!("flow_b_{}", l + 1),
                "<i4".into(),
                b_le!(py, r.flow_b[l], 4),
            ));
        }
        for l in 0..levels {
            out.push((
                format!("flow_a_{}", l + 1),
                "<i4".into(),
                b_le!(py, r.flow_a[l], 4),
            ));
        }
        out.extend::<Vec<(String, String, Py<PyBytes>)>>(vec![
            ("t_buy".into(), "<u4".into(), b_le!(py, r.t_buy, 4)),
            ("t_sell".into(), "<u4".into(), b_le!(py, r.t_sell, 4)),
            ("t_buy_n".into(), "<u4".into(), b_le!(py, r.t_buy_n, 4)),
            ("t_sell_n".into(), "<u4".into(), b_le!(py, r.t_sell_n, 4)),
            ("t_px_high".into(), "<i8".into(), b_le!(py, r.t_px_high, 8)),
            ("t_px_low".into(), "<i8".into(), b_le!(py, r.t_px_low, 8)),
            ("add_best_b".into(), "<u4".into(), b_le!(py, r.add_best_b, 4)),
            ("add_near_b".into(), "<u4".into(), b_le!(py, r.add_near_b, 4)),
            ("pull_best_b".into(), "<u4".into(), b_le!(py, r.pull_best_b, 4)),
            ("pull_near_b".into(), "<u4".into(), b_le!(py, r.pull_near_b, 4)),
            ("add_best_a".into(), "<u4".into(), b_le!(py, r.add_best_a, 4)),
            ("add_near_a".into(), "<u4".into(), b_le!(py, r.add_near_a, 4)),
            ("pull_best_a".into(), "<u4".into(), b_le!(py, r.pull_best_a, 4)),
            ("pull_near_a".into(), "<u4".into(), b_le!(py, r.pull_near_a, 4)),
            ("fill_b".into(), "<u4".into(), b_le!(py, r.fill_b, 4)),
            ("fill_a".into(), "<u4".into(), b_le!(py, r.fill_a, 4)),
            ("hidden_b".into(), "<u4".into(), b_le!(py, r.hidden_b, 4)),
            ("hidden_a".into(), "<u4".into(), b_le!(py, r.hidden_a, 4)),
            ("term_filled".into(), "<u4".into(), b_le!(py, r.term_filled, 4)),
            (
                "term_pulled_touched".into(),
                "<u4".into(),
                b_le!(py, r.term_pulled_touched, 4),
            ),
            (
                "term_pulled_untouched".into(),
                "<u4".into(),
                b_le!(py, r.term_pulled_untouched, 4),
            ),
            (
                "term_filled_refill".into(),
                "<u4".into(),
                b_le!(py, r.term_filled_refill, 4),
            ),
            ("life_filled_sum".into(), "<u8".into(), b_le!(py, r.life_filled_sum, 8)),
            ("life_pulled_sum".into(), "<u8".into(), b_le!(py, r.life_pulled_sum, 8)),
            (
                "term_filled_unchained".into(),
                "<u4".into(),
                b_le!(py, r.term_filled_unchained, 4),
            ),
            (
                "life_filled_unchained_sum".into(),
                "<u8".into(),
                b_le!(py, r.life_filled_unchained_sum, 8),
            ),
            (
                "term_pulled_unchained".into(),
                "<u4".into(),
                b_le!(py, r.term_pulled_unchained, 4),
            ),
            (
                "life_pulled_unchained_sum".into(),
                "<u8".into(),
                b_le!(py, r.life_pulled_unchained_sum, 8),
            ),
            ("refill_b".into(), "<u4".into(), b_le!(py, r.refill_b, 4)),
            ("refill_a".into(), "<u4".into(), b_le!(py, r.refill_a, 4)),
            ("refill_conf_sum".into(), "<f4".into(), b_le!(py, r.refill_conf_sum, 4)),
            ("age_best_b".into(), "<u8".into(), b_le!(py, r.age_best_b, 8)),
            ("age_best_a".into(), "<u8".into(), b_le!(py, r.age_best_a, 8)),
        ]);
        Ok(out)
    }

    /// Drain completed lifecycle records as raw little-endian column
    /// buffers: [(column_name, numpy_dtype, bytes)]. The tracker keeps
    /// running; call after finish() for a full session, or periodically.
    fn lifecycle_drain(&mut self, py: Python<'_>) -> PyResult<Vec<(String, String, Py<PyBytes>)>> {
        let Some(tr) = self.inner.lifecycle.as_mut() else {
            return Err(PyValueError::new_err(
                "lifecycle tracking is disabled (construct with lifecycle=True)",
            ));
        };
        let r = tr.drain();

        fn b_u8(py: Python<'_>, v: &[u8]) -> Py<PyBytes> {
            PyBytes::new(py, v).unbind()
        }
        macro_rules! b_le {
            ($py:expr, $v:expr, $w:expr) => {{
                let mut buf = Vec::with_capacity($v.len() * $w);
                for x in $v.iter() {
                    buf.extend_from_slice(&x.to_le_bytes());
                }
                PyBytes::new($py, &buf).unbind()
            }};
        }
        macro_rules! cols {
            ($(($name:expr, $dt:expr, $bytes:expr)),+ $(,)?) => {
                vec![$(($name.to_string(), $dt.to_string(), $bytes)),+]
            };
        }
        Ok(cols![
            ("instrument_id", "<u4", b_le!(py, r.instrument_id, 4)),
            ("order_id", "<u8", b_le!(py, r.order_id, 8)),
            ("side", "u1", b_u8(py, &r.side)),
            ("price_at_add", "<i8", b_le!(py, r.price_at_add, 8)),
            ("price_final", "<i8", b_le!(py, r.price_final, 8)),
            ("ts_added", "<u8", b_le!(py, r.ts_added, 8)),
            ("ts_terminated", "<u8", b_le!(py, r.ts_terminated, 8)),
            ("from_snapshot", "u1", b_u8(py, &r.from_snapshot)),
            ("entered_unknown_modify", "u1", b_u8(py, &r.entered_unknown_modify)),
            ("initial_size", "<u4", b_le!(py, r.initial_size, 4)),
            ("max_size", "<u4", b_le!(py, r.max_size, 4)),
            ("final_size", "<u4", b_le!(py, r.final_size, 4)),
            ("filled_size", "<u4", b_le!(py, r.filled_size, 4)),
            ("cancelled_size", "<u8", b_le!(py, r.cancelled_size, 8)),
            ("n_size_increases", "<u4", b_le!(py, r.n_size_increases, 4)),
            ("n_size_decreases", "<u4", b_le!(py, r.n_size_decreases, 4)),
            ("n_price_changes", "<u4", b_le!(py, r.n_price_changes, 4)),
            ("final_state", "u1", b_u8(py, &r.final_state)),
            ("queue_pos_at_add", "<u4", b_le!(py, r.queue_pos_at_add, 4)),
            ("vol_ahead_at_add", "<u8", b_le!(py, r.vol_ahead_at_add, 8)),
            ("queue_pos_at_term", "<u4", b_le!(py, r.queue_pos_at_term, 4)),
            ("dist_same_at_add", "<i8", b_le!(py, r.dist_same_at_add, 8)),
            ("dist_mid2_at_add", "<i8", b_le!(py, r.dist_mid2_at_add, 8)),
            ("dist_same_at_term", "<i8", b_le!(py, r.dist_same_at_term, 8)),
            ("dist_mid2_at_term", "<i8", b_le!(py, r.dist_mid2_at_term, 8)),
            ("min_dist_same", "<i8", b_le!(py, r.min_dist_same, 8)),
            ("chain_id", "<u8", b_le!(py, r.chain_id, 8)),
            ("chain_index", "<u4", b_le!(py, r.chain_index, 4)),
            ("link_confidence", "<f4", b_le!(py, r.link_confidence, 4)),
            ("link_dt_ns", "<u8", b_le!(py, r.link_dt_ns, 8)),
        ])
    }
}

/// Lean full-file scan for the Step 12.5 roll ledger: per-instrument
/// (records, T volume) with no book maintenance. Same decoder as replay
/// (one code path for reading), GIL released. Returns rows sorted by
/// instrument_id: [(instrument_id, records, t_volume)].
#[pyfunction]
fn scan_t_volumes(py: Python<'_>, path: &str) -> PyResult<Vec<(u32, u64, u64)>> {
    let path = path.to_owned();
    py.detach(move || {
        let mut dec = dbn::decode::dbn::Decoder::from_zstd_file(&path)
            .map_err(|e| PyIOError::new_err(format!("open {}: {}", path, e)))?;
        let mut acc: std::collections::BTreeMap<u32, (u64, u64)> = std::collections::BTreeMap::new();
        loop {
            match dec.decode_record_ref() {
                Ok(Some(rec)) => {
                    if let Some(m) = rec.get::<dbn::MboMsg>() {
                        let e = acc.entry(m.hd.instrument_id).or_insert((0, 0));
                        e.0 += 1;
                        if m.action as u8 == b'T' {
                            e.1 += m.size as u64;
                        }
                    }
                }
                Ok(None) => break,
                Err(e) => return Err(PyIOError::new_err(format!("decode error: {}", e))),
            }
        }
        Ok(acc.into_iter().map(|(iid, (n, v))| (iid, n, v)).collect())
    })
}

#[pymodule]
fn gc_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<MboEngine>()?;
    m.add_function(wrap_pyfunction!(scan_t_volumes, m)?)?;
    m.add("UNDEF_PRICE", types::UNDEF_PRICE)?;
    m.add("F_LAST", types::F_LAST)?;
    m.add("F_SNAPSHOT", types::F_SNAPSHOT)?;
    // Phase 2 lifecycle terminal states (neutral mechanics, never intent)
    m.add("STATE_FILLED", lifecycle::STATE_FILLED)?;
    m.add("STATE_PARTIAL_CANCELLED", lifecycle::STATE_PARTIAL_CANCELLED)?;
    m.add("STATE_CANCELLED", lifecycle::STATE_CANCELLED)?;
    m.add("STATE_CLEARED", lifecycle::STATE_CLEARED)?;
    m.add("STATE_END_OF_DATA", lifecycle::STATE_END_OF_DATA)?;
    m.add("STATE_REPLACED", lifecycle::STATE_REPLACED)?;
    m.add("POS_SENTINEL", lifecycle::POS_SENTINEL)?;
    m.add("DIST_SENTINEL", lifecycle::DIST_SENTINEL)?;
    m.add("MIN_DIST_SENTINEL", lifecycle::MIN_DIST_SENTINEL)?;
    Ok(())
}
