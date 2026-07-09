//! MBO event engine (plan Phase 1, Steps 3-4).
//!
//! Processes Databento MBO actions — Add, Cancel, Modify, Trade, Fill,
//! cleaR — across all instruments of the feed, maintaining the order store
//! and price-level book, with:
//!   - matching-event grouping via ts_event + F_LAST (CME event boundaries),
//!   - the Trade/Fill reconciliation rule (T-only aggressive volume; F only
//!     updates resting state; per-event-group volume reconciliation),
//!   - duplicate / out-of-order / unknown-order protection (counted, logged),
//!   - R1 engine invariants (crossed book at group end; periodic order-store
//!     vs level-aggregate audit),
//!   - deterministic state digest for the replay-twice CI test.

use std::collections::BTreeMap;

use rustc_hash::FxHashMap;

use crate::book::InstrumentBook;
use crate::lifecycle::{
    LifecycleConfig, Tracker, POS_SENTINEL, STATE_CANCELLED, STATE_CLEARED, STATE_END_OF_DATA,
    STATE_FILLED, STATE_REPLACED,
};
use crate::types::{
    Fnv1a, Incident, IncidentKind, Order, OrderState, F_LAST, F_SNAPSHOT, SIDE_ASK, SIDE_BID,
};

const N_KINDS: usize = 10;

#[derive(Default, Clone)]
pub struct Counters {
    pub records: u64,
    pub adds: u64,
    pub cancels: u64,
    pub modifies: u64,
    pub trades: u64,
    pub fills: u64,
    pub clears: u64,
    pub others: u64,
    pub snapshot_records: u64,
    pub t_volume: u64,
    pub t_volume_buy: u64,
    pub t_volume_sell: u64,
    pub f_volume: u64,
    /// F volume against order ids not in the store (unknown fills) — the
    /// only F volume that cannot appear in lifecycle fill attributions
    pub f_volume_unattributed: u64,
    /// C records that complete an execution (an F for the same order in the
    /// same matching event) — NOT trader cancels (Phase 2 depends on this).
    pub cancels_fill_removal: u64,
    /// C records that pull liquidity (true cancels)
    pub cancels_pulled: u64,
    pub event_groups: u64,
    /// F records whose size exceeds the order's displayed size — iceberg
    /// executions (hidden quantity); a signal, not an error.
    pub fills_exceeding_displayed: u64,
    /// groups where T volume == F volume > 0 (fully reconciled executions)
    pub groups_tf_matched: u64,
    /// groups where F volume == 2 x T volume: auction uncross — both resting
    /// sides receive fill attribution while T reports traded volume once.
    pub groups_tf_matched_auction: u64,
    /// groups with T volume but zero F volume (e.g. implied/spread executions)
    pub groups_t_without_f: u64,
    /// groups with both T and F volume that disagree (true mismatches)
    pub groups_tf_mismatch: u64,
    /// groups with F volume but zero T volume
    pub groups_f_without_t: u64,
    pub incident_counts: [u64; N_KINDS],
}

struct GroupAccum {
    ts_event: u64,
    /// per-instrument (t_volume, f_volume) within the current matching event
    per_instrument: Vec<(u32, u64, u64)>,
    touched: Vec<u32>,
}

impl GroupAccum {
    fn new() -> Self {
        GroupAccum {
            ts_event: u64::MAX,
            per_instrument: Vec::with_capacity(8),
            touched: Vec::with_capacity(8),
        }
    }
    fn reset(&mut self, ts: u64) {
        self.ts_event = ts;
        self.per_instrument.clear();
        self.touched.clear();
    }
    fn add_t(&mut self, iid: u32, size: u64) {
        match self.per_instrument.iter_mut().find(|e| e.0 == iid) {
            Some(e) => e.1 += size,
            None => self.per_instrument.push((iid, size, 0)),
        }
    }
    fn add_f(&mut self, iid: u32, size: u64) {
        match self.per_instrument.iter_mut().find(|e| e.0 == iid) {
            Some(e) => e.2 += size,
            None => self.per_instrument.push((iid, 0, size)),
        }
    }
    fn touch(&mut self, iid: u32) {
        if !self.touched.contains(&iid) {
            self.touched.push(iid);
        }
    }
}

pub struct Engine {
    pub books: FxHashMap<u32, InstrumentBook>,
    pub counters: Counters,
    incidents: Vec<Incident>,
    max_incidents: usize,
    group: GroupAccum,
    last_seq_by_channel: FxHashMap<u8, u32>,
    audit_interval: u64,
    next_audit_at: u64,
    /// record counts per instrument (BTreeMap => deterministic listing)
    pub records_per_instrument: BTreeMap<u32, u64>,
    pub t_volume_per_instrument: BTreeMap<u32, u64>,
    pub halt_on_engine_invariant: bool,
    pub halted: Option<String>,
    /// Phase 2 order-lifecycle/queue tracker (None = Phase 1 behavior).
    pub lifecycle: Option<Tracker>,
    last_ts: u64,
    lifecycle_finalized: bool,
}

impl Engine {
    pub fn new(
        max_incidents: usize,
        audit_interval: u64,
        halt_on_engine_invariant: bool,
        lifecycle: Option<LifecycleConfig>,
    ) -> Self {
        Engine {
            books: FxHashMap::default(),
            counters: Counters::default(),
            incidents: Vec::new(),
            max_incidents,
            group: GroupAccum::new(),
            last_seq_by_channel: FxHashMap::default(),
            audit_interval,
            next_audit_at: audit_interval,
            records_per_instrument: BTreeMap::new(),
            t_volume_per_instrument: BTreeMap::new(),
            halt_on_engine_invariant,
            halted: None,
            lifecycle: lifecycle.map(Tracker::new),
            last_ts: 0,
            lifecycle_finalized: false,
        }
    }

    fn incident(
        &mut self,
        kind: IncidentKind,
        ts_event: u64,
        instrument_id: u32,
        order_id: u64,
        sequence: u32,
        detail: String,
    ) {
        self.counters.incident_counts[kind as usize] += 1;
        if self.incidents.len() < self.max_incidents {
            self.incidents.push(Incident {
                kind,
                ts_event,
                instrument_id,
                order_id,
                sequence,
                detail,
            });
        }
    }

    fn flush_group(&mut self) {
        if self.group.ts_event == u64::MAX {
            return;
        }
        self.counters.event_groups += 1;
        let per_instrument = std::mem::take(&mut self.group.per_instrument);
        let ts = self.group.ts_event;
        for (iid, t, f) in per_instrument.iter().copied() {
            if t > 0 && f > 0 {
                if t == f {
                    self.counters.groups_tf_matched += 1;
                } else if f == 2 * t {
                    self.counters.groups_tf_matched_auction += 1;
                } else {
                    self.counters.groups_tf_mismatch += 1;
                    self.incident(
                        IncidentKind::TfReconcileMismatch,
                        ts,
                        iid,
                        0,
                        0,
                        format!("T={} F={}", t, f),
                    );
                }
            } else if t > 0 {
                // No resting-order fills on this instrument for the trade —
                // expected for implied/spread executions; counted, not logged.
                self.counters.groups_t_without_f += 1;
            } else if f > 0 {
                self.counters.groups_f_without_t += 1;
                self.incident(
                    IncidentKind::TfReconcileMismatch,
                    ts,
                    iid,
                    0,
                    0,
                    format!("F={} with no T", f),
                );
            }
        }
        // crossed-book invariant at matching-event end (R1); legitimate
        // transient crossings inside a group are not flagged.
        let touched = std::mem::take(&mut self.group.touched);
        for iid in touched {
            if let Some(b) = self.books.get(&iid) {
                if let (Some((bb, _)), Some((ba, _))) = (b.best_bid(), b.best_ask()) {
                    if bb >= ba {
                        self.incident(
                            IncidentKind::CrossedBook,
                            ts,
                            iid,
                            0,
                            0,
                            format!("bid {} >= ask {}", bb, ba),
                        );
                    }
                }
            }
        }
        self.group.ts_event = u64::MAX;
    }

    /// Full order-store vs level-aggregate audit over every instrument
    /// (R1: engine-bug detector; halts by default).
    fn run_store_audit(&mut self, ts: u64) {
        let mut failure: Option<(u32, String)> = None;
        for (iid, b) in self.books.iter() {
            if let Err(e) = b.views_consistent() {
                failure = Some((*iid, e));
                break;
            }
        }
        if let Some((iid, e)) = failure {
            self.incident(IncidentKind::StoreLevelsMismatch, ts, iid, 0, 0, e.clone());
            if self.halt_on_engine_invariant {
                self.halted = Some(format!("store/levels mismatch on instrument {}: {}", iid, e));
            }
        }
    }

    /// Process one MBO record. Returns false once halted.
    #[allow(clippy::too_many_arguments)]
    #[inline]
    pub fn process(
        &mut self,
        ts_event: u64,
        price: i64,
        size: u32,
        order_id: u64,
        flags: u8,
        channel_id: u8,
        action: u8,
        side: u8,
        sequence: u32,
        instrument_id: u32,
    ) -> bool {
        if self.halted.is_some() {
            return false;
        }
        self.counters.records += 1;
        *self.records_per_instrument.entry(instrument_id).or_insert(0) += 1;

        // matching-event boundary: ts_event change starts a new group
        if ts_event != self.group.ts_event {
            self.flush_group();
            self.group.reset(ts_event);
        }
        self.group.touch(instrument_id);

        let is_snapshot = flags & F_SNAPSHOT != 0;
        if is_snapshot {
            self.counters.snapshot_records += 1;
        }

        // out-of-order/duplicate protection: per-channel sequence regression
        // (snapshot records carry original sequences and are exempt)
        if sequence != 0 && !is_snapshot {
            let last = self.last_seq_by_channel.entry(channel_id).or_insert(0);
            if sequence < *last {
                let last_val = *last;
                self.incident(
                    IncidentKind::SequenceRegression,
                    ts_event,
                    instrument_id,
                    order_id,
                    sequence,
                    format!("seq {} < last {} on channel {}", sequence, last_val, channel_id),
                );
            } else {
                *last = sequence;
            }
        }

        self.last_ts = ts_event;
        match action {
            b'R' => {
                self.counters.clears += 1;
                let book = self.books.entry(instrument_id).or_default();
                if let Some(tr) = self.lifecycle.as_mut() {
                    // sweep every resting order out as Cleared (sorted ids =>
                    // deterministic emission), then reset instrument tracking
                    let bests = book.best_prices();
                    let mut ids: Vec<u64> = book.orders.keys().copied().collect();
                    ids.sort_unstable();
                    for oid in ids {
                        let o = book.orders[&oid];
                        tr.on_terminate(
                            instrument_id,
                            oid,
                            &o,
                            STATE_CLEARED,
                            POS_SENTINEL,
                            ts_event,
                            bests,
                        );
                    }
                    tr.on_clear(instrument_id);
                }
                book.clear();
            }
            b'A' => {
                self.counters.adds += 1;
                let order = Order {
                    side,
                    price,
                    current_size: size,
                    initial_size: size,
                    ts_added: ts_event,
                    ts_last_updated: ts_event,
                    state: OrderState::Active,
                    from_snapshot: is_snapshot,
                    filled_size: 0,
                    unapplied_fill: 0,
                    last_fill_ts: 0,
                };
                let duplicate;
                {
                    let book = self.books.entry(instrument_id).or_default();
                    let out = book.add_order(order_id, order);
                    duplicate = out.replaced.is_some();
                    if let Some(tr) = self.lifecycle.as_mut() {
                        let bests = book.best_prices();
                        if let Some((old, pos)) = out.replaced {
                            tr.on_terminate(
                                instrument_id,
                                order_id,
                                &old,
                                STATE_REPLACED,
                                pos.map(|p| p as u32).unwrap_or(POS_SENTINEL),
                                ts_event,
                                bests,
                            );
                        }
                        tr.on_add(
                            instrument_id,
                            order_id,
                            &order,
                            out.queue_pos,
                            out.vol_ahead,
                            bests,
                            false,
                        );
                        tr.on_bests(instrument_id, ts_event, bests);
                    }
                }
                if duplicate {
                    self.incident(
                        IncidentKind::DuplicateAdd,
                        ts_event,
                        instrument_id,
                        order_id,
                        sequence,
                        String::new(),
                    );
                }
            }
            b'C' => {
                self.counters.cancels += 1;
                let mut size_mismatch: Option<u32> = None;
                let mut unknown = false;
                {
                    let book = self.books.entry(instrument_id).or_default();
                    // market view with the order still resting (Phase 2
                    // distance-at-cancel is measured before removal)
                    let bests_before = if self.lifecycle.is_some() {
                        book.best_prices()
                    } else {
                        (None, None)
                    };
                    match book.cancel_order(order_id) {
                        Some((o, pos)) => {
                            // fill-removal vs trader pull: an F for this order
                            // in the SAME matching event means this C completes
                            // an execution, not a cancellation.
                            let fill_removal = o.last_fill_ts == ts_event;
                            if fill_removal {
                                self.counters.cancels_fill_removal += 1;
                            } else {
                                self.counters.cancels_pulled += 1;
                            }
                            if size != 0 && o.current_size != size {
                                size_mismatch = Some(o.current_size);
                            }
                            if let Some(tr) = self.lifecycle.as_mut() {
                                let state = if fill_removal {
                                    STATE_FILLED
                                } else {
                                    STATE_CANCELLED // partial-vs-pure resolved in tracker
                                };
                                tr.on_terminate(
                                    instrument_id,
                                    order_id,
                                    &o,
                                    state,
                                    pos.map(|p| p as u32).unwrap_or(POS_SENTINEL),
                                    ts_event,
                                    bests_before,
                                );
                                tr.on_bests(instrument_id, ts_event, book.best_prices());
                            }
                        }
                        None => unknown = true,
                    }
                }
                if let Some(stored) = size_mismatch {
                    self.incident(
                        IncidentKind::CancelSizeMismatch,
                        ts_event,
                        instrument_id,
                        order_id,
                        sequence,
                        format!("record size {} stored {}", size, stored),
                    );
                }
                if unknown {
                    self.incident(
                        IncidentKind::UnknownCancel,
                        ts_event,
                        instrument_id,
                        order_id,
                        sequence,
                        String::new(),
                    );
                }
            }
            b'M' => {
                self.counters.modifies += 1;
                let mut unknown = false;
                {
                    let book = self.books.entry(instrument_id).or_default();
                    match book.modify_order(order_id, price, size, ts_event) {
                        Some((o_side, old_price, old_size, last_fill_ts)) => {
                            if let Some(tr) = self.lifecycle.as_mut() {
                                // an M reducing size with an F in the same
                                // matching event applies a partial fill — not
                                // a voluntary (trader) reduction
                                let fill_application =
                                    last_fill_ts == ts_event && size < old_size;
                                let bests = book.best_prices();
                                tr.on_modify(
                                    instrument_id,
                                    order_id,
                                    o_side,
                                    old_price,
                                    price,
                                    old_size,
                                    size,
                                    ts_event,
                                    fill_application,
                                    bests,
                                );
                                tr.on_bests(instrument_id, ts_event, bests);
                            }
                        }
                        None => {
                            // unknown modify: treated as an add to keep the
                            // book as complete as possible; logged as a
                            // data-quality incident
                            unknown = true;
                            let order = Order {
                                side,
                                price,
                                current_size: size,
                                initial_size: size,
                                ts_added: ts_event,
                                ts_last_updated: ts_event,
                                state: OrderState::Active,
                                from_snapshot: false,
                                filled_size: 0,
                                unapplied_fill: 0,
                                last_fill_ts: 0,
                            };
                            let out = book.add_order(order_id, order);
                            if let Some(tr) = self.lifecycle.as_mut() {
                                let bests = book.best_prices();
                                tr.on_add(
                                    instrument_id,
                                    order_id,
                                    &order,
                                    out.queue_pos,
                                    out.vol_ahead,
                                    bests,
                                    true,
                                );
                                tr.on_bests(instrument_id, ts_event, bests);
                            }
                        }
                    }
                }
                if unknown {
                    self.incident(
                        IncidentKind::UnknownModify,
                        ts_event,
                        instrument_id,
                        order_id,
                        sequence,
                        String::new(),
                    );
                }
            }
            b'T' => {
                // Aggressive volume/delta come ONLY from T actions (plan rule);
                // T never mutates the book.
                self.counters.trades += 1;
                self.counters.t_volume += size as u64;
                match side {
                    SIDE_BID => self.counters.t_volume_buy += size as u64,
                    SIDE_ASK => self.counters.t_volume_sell += size as u64,
                    _ => {}
                }
                *self
                    .t_volume_per_instrument
                    .entry(instrument_id)
                    .or_insert(0) += size as u64;
                self.group.add_t(instrument_id, size as u64);
            }
            b'F' => {
                // F records ONLY update resting-order state (plan rule).
                // Verified GLBX semantics: F is execution attribution; the
                // book mutation arrives as the follow-up C (full fill) or M
                // (partial fill) in the same matching event.
                self.counters.fills += 1;
                self.counters.f_volume += size as u64;
                self.group.add_f(instrument_id, size as u64);
                let book = self.books.entry(instrument_id).or_default();
                let (found, over) = book.record_fill(order_id, size, ts_event);
                if !found {
                    self.counters.f_volume_unattributed += size as u64;
                    self.incident(
                        IncidentKind::UnknownFill,
                        ts_event,
                        instrument_id,
                        order_id,
                        sequence,
                        String::new(),
                    );
                } else if over {
                    // hidden (iceberg) quantity executing — a signal for the
                    // Phase 2/3 iceberg features, not a data-quality incident
                    self.counters.fills_exceeding_displayed += 1;
                }
            }
            _ => {
                self.counters.others += 1;
            }
        }

        if flags & F_LAST != 0 {
            self.flush_group();
        }

        if self.counters.records >= self.next_audit_at {
            self.next_audit_at += self.audit_interval;
            self.run_store_audit(ts_event);
        }
        self.halted.is_none()
    }

    /// Flush any open matching-event group (call at end of stream) and, with
    /// lifecycle tracking on, emit an EndOfData record for every order still
    /// resting (idempotent; sorted instrument/order ids => deterministic).
    /// The books themselves stay intact and queryable (Milestone 1 views).
    pub fn finish(&mut self) {
        self.flush_group();
        if self.lifecycle_finalized {
            return;
        }
        self.lifecycle_finalized = true;
        let Some(tr) = self.lifecycle.as_mut() else {
            return;
        };
        let ts = self.last_ts;
        let mut iids: Vec<u32> = self.books.keys().copied().collect();
        iids.sort_unstable();
        for iid in iids {
            let book = &self.books[&iid];
            let bests = book.best_prices();
            // every order's FIFO position in one O(N) sweep
            let mut pos: FxHashMap<u64, u32> = FxHashMap::default();
            for side in [&book.bids, &book.asks] {
                for lvl in side.levels.values() {
                    for (i, &id) in lvl.fifo.iter().enumerate() {
                        pos.insert(id, i as u32);
                    }
                }
            }
            let mut ids: Vec<u64> = book.orders.keys().copied().collect();
            ids.sort_unstable();
            for oid in ids {
                let o = book.orders[&oid];
                tr.on_terminate(
                    iid,
                    oid,
                    &o,
                    STATE_END_OF_DATA,
                    pos.get(&oid).copied().unwrap_or(POS_SENTINEL),
                    ts,
                    bests,
                );
            }
        }
    }

    /// Deterministic digest over the full engine state (all instruments,
    /// both views) plus record counters.
    pub fn state_digest(&self) -> u64 {
        let mut h = Fnv1a::new();
        let mut ids: Vec<u32> = self.books.keys().copied().collect();
        ids.sort_unstable();
        for iid in ids {
            h.write_u64(iid as u64);
            self.books[&iid].digest(&mut h);
        }
        h.write_u64(self.counters.records);
        h.write_u64(self.counters.t_volume);
        h.write_u64(self.counters.f_volume);
        h.write_u64(self.counters.event_groups);
        h.finish()
    }

    pub fn incidents(&self) -> &[Incident] {
        &self.incidents
    }

    pub fn stats_pairs(&self) -> Vec<(&'static str, u64)> {
        let c = &self.counters;
        let mut v = vec![
            ("records", c.records),
            ("adds", c.adds),
            ("cancels", c.cancels),
            ("modifies", c.modifies),
            ("trades", c.trades),
            ("fills", c.fills),
            ("clears", c.clears),
            ("others", c.others),
            ("snapshot_records", c.snapshot_records),
            ("t_volume", c.t_volume),
            ("t_volume_buy", c.t_volume_buy),
            ("t_volume_sell", c.t_volume_sell),
            ("f_volume", c.f_volume),
            ("f_volume_unattributed", c.f_volume_unattributed),
            ("cancels_fill_removal", c.cancels_fill_removal),
            ("cancels_pulled", c.cancels_pulled),
            ("event_groups", c.event_groups),
            ("fills_exceeding_displayed", c.fills_exceeding_displayed),
            ("groups_tf_matched", c.groups_tf_matched),
            ("groups_tf_matched_auction", c.groups_tf_matched_auction),
            ("groups_t_without_f", c.groups_t_without_f),
            ("groups_tf_mismatch", c.groups_tf_mismatch),
            ("groups_f_without_t", c.groups_f_without_t),
        ];
        for k in 0..N_KINDS {
            let kind: IncidentKind = unsafe { std::mem::transmute(k as u8) };
            v.push((kind.name(), c.incident_counts[k]));
        }
        v
    }
}
