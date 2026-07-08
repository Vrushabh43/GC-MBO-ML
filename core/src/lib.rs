//! gc_core — compiled performance core for the GC order-flow system.
//!
//! Phase 0 Option A: Rust + PyO3. One code path for historical and live:
//! the same `Engine::process` handles records from the bundled DBN file
//! decoder (historical replay) and records pushed one-by-one from Python
//! (synthetic tests today, live feed later).

mod book;
mod engine;
mod types;

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;

use dbn::decode::DecodeRecordRef;

use engine::Engine;
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
    #[new]
    #[pyo3(signature = (max_incidents=10000, audit_interval=1_000_000, halt_on_engine_invariant=true))]
    fn new(max_incidents: usize, audit_interval: u64, halt_on_engine_invariant: bool) -> Self {
        MboEngine {
            inner: Engine::new(max_incidents, audit_interval, halt_on_engine_invariant),
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
}

#[pymodule]
fn gc_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<MboEngine>()?;
    m.add("UNDEF_PRICE", types::UNDEF_PRICE)?;
    m.add("F_LAST", types::F_LAST)?;
    m.add("F_SNAPSHOT", types::F_SNAPSHOT)?;
    Ok(())
}
