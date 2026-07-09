//! Order lifecycle + queue tracking (plan Phase 2, Step 6).
//!
//! Runs inside the one Engine code path (Critical Rule 3): the tracker is
//! notified from `Engine::process` after each book mutation and emits one
//! lifecycle record per order at its terminal event. It never mutates the
//! book; it only observes.
//!
//! Per order it records: add/termination timestamps and prices, initial /
//! max / final / filled / cancelled size, modify counts (Globex priority
//! semantics: size decrease keeps priority, size increase and price change
//! lose it — enforced by book.rs; counted here), FIFO queue position and
//! volume ahead at add, FIFO position at termination, distance from the
//! same-side best and from mid at add and at termination, the closest the
//! same-side best came to the order while it rested ("survival as price
//! approached"), and the terminal state.
//!
//! CME iceberg refill heuristic (plan Phase 2 [NEW]): when a displayed clip
//! is exhausted (fill-removal `C`), the refill arrives as a NEW order id at
//! the back of the queue. An Add at the same price/side within
//! `iceberg_window_ns` with size <= `clip_tol` x the parent's displayed clip
//! is linked into the parent's chain (`chain_id` = root order id). The link
//! is a HEURISTIC: a confidence in (0, 1] is stored with every link and the
//! chain must never be treated as fact (Critical Rule 8).
//!
//! Neutral naming (plan Phase 2): states and fields describe mechanics
//! (pulled, fill-removal, cancel-before-touch); behavior is never labeled
//! as intent (no "spoofing").
//!
//! Determinism: records are emitted in event order; end-of-data / clear
//! sweeps emit in sorted (instrument_id, order_id) order; a running FNV-1a
//! digest over every emitted field supports the replay-twice CI test.

use std::collections::VecDeque;

use rustc_hash::FxHashMap;

use crate::types::{Fnv1a, Order, SIDE_BID};

/// Terminal states (neutral mechanics, never intent).
pub const STATE_FILLED: u8 = 0; // removed by fill (C completing an execution)
pub const STATE_PARTIAL_CANCELLED: u8 = 1; // pulled after partial execution
pub const STATE_CANCELLED: u8 = 2; // pulled, never executed
pub const STATE_CLEARED: u8 = 3; // removed by book clear (R)
pub const STATE_END_OF_DATA: u8 = 4; // still resting when the stream ended
pub const STATE_REPLACED: u8 = 5; // displaced by a duplicate add (data anomaly)

pub const POS_SENTINEL: u32 = u32::MAX; // queue position unknown
pub const DIST_SENTINEL: i64 = i64::MIN; // distance undefined (side/mid missing)
pub const MIN_DIST_SENTINEL: i64 = i64::MAX; // closest approach never measurable

#[derive(Clone, Copy)]
pub struct LifecycleConfig {
    pub iceberg_window_ns: u64,
    pub iceberg_clip_tol: f64,
}

/// Live (not yet terminated) per-order lifecycle state, parallel to the
/// order store. Everything already in `Order` is not duplicated here.
struct Live {
    price_at_add: i64,
    queue_pos_at_add: u32,
    vol_ahead_at_add: u64,
    dist_same_at_add: i64,
    dist_mid2_at_add: i64,
    /// start of the current price segment (changes on price modify)
    seg_start_ts: u64,
    /// same-side best prevailing at segment start (the approach deque only
    /// holds post-start changes; the prevailing value must be carried)
    seg_prevail: Option<i64>,
    /// min distance from same-side best over finalized segments
    min_dist_same: i64,
    n_size_increases: u32,
    n_size_decreases: u32,
    n_price_changes: u32,
    cancelled_size: u64,
    max_size: u32,
    entered_unknown_modify: bool,
    chain_id: u64,
    chain_index: u32,
    link_confidence: f32,
    link_dt_ns: u64,
}

/// Suffix-extreme tracker for one instrument side: answers
/// "what is the min (bids) / max (asks) best price since time t?"
/// Monotonic deque of (ts, best) — ts ascending; for bids prices ascending
/// (suffix minima), for asks prices descending (suffix maxima).
#[derive(Default)]
struct ApproachSide {
    deque: VecDeque<(u64, i64)>,
}

impl ApproachSide {
    fn push_min(&mut self, ts: u64, best: i64) {
        while matches!(self.deque.back(), Some(&(_, p)) if p >= best) {
            self.deque.pop_back();
        }
        self.deque.push_back((ts, best));
    }

    fn push_max(&mut self, ts: u64, best: i64) {
        while matches!(self.deque.back(), Some(&(_, p)) if p <= best) {
            self.deque.pop_back();
        }
        self.deque.push_back((ts, best));
    }

    /// Extreme of the tracked best over best-price CHANGES at or after
    /// since_ts. None if the best did not change in the window — the caller
    /// combines this with the value prevailing at since_ts (`seg_prevail`),
    /// which this deque cannot know (it may have been set long before).
    fn extreme_since(&self, since_ts: u64) -> Option<i64> {
        let idx = self.deque.partition_point(|&(t, _)| t < since_ts);
        if idx < self.deque.len() {
            Some(self.deque[idx].1)
        } else {
            None
        }
    }

    fn clear(&mut self) {
        self.deque.clear();
    }
}

/// Pending iceberg-refill slot: a displayed clip at (side, price) was just
/// exhausted; the next qualifying Add continues the chain.
struct RefillCandidate {
    ts: u64,
    chain_id: u64,
    chain_index: u32,
    clip: u32,
}

#[derive(Default)]
struct InstrumentTracker {
    live: FxHashMap<u64, Live>,
    approach_bid: ApproachSide,
    approach_ask: ApproachSide,
    last_best_bid: Option<i64>,
    last_best_ask: Option<i64>,
    refill: FxHashMap<(u8, i64), RefillCandidate>,
}

/// Completed lifecycle records, struct-of-vectors (drained to Python as
/// raw little-endian column buffers).
#[derive(Default)]
pub struct Records {
    pub instrument_id: Vec<u32>,
    pub order_id: Vec<u64>,
    pub side: Vec<u8>,
    pub price_at_add: Vec<i64>,
    pub price_final: Vec<i64>,
    pub ts_added: Vec<u64>,
    pub ts_terminated: Vec<u64>,
    pub from_snapshot: Vec<u8>,
    pub entered_unknown_modify: Vec<u8>,
    pub initial_size: Vec<u32>,
    pub max_size: Vec<u32>,
    pub final_size: Vec<u32>,
    pub filled_size: Vec<u32>,
    pub cancelled_size: Vec<u64>,
    pub n_size_increases: Vec<u32>,
    pub n_size_decreases: Vec<u32>,
    pub n_price_changes: Vec<u32>,
    pub final_state: Vec<u8>,
    pub queue_pos_at_add: Vec<u32>,
    pub vol_ahead_at_add: Vec<u64>,
    pub queue_pos_at_term: Vec<u32>,
    pub dist_same_at_add: Vec<i64>,
    pub dist_mid2_at_add: Vec<i64>,
    pub dist_same_at_term: Vec<i64>,
    pub dist_mid2_at_term: Vec<i64>,
    pub min_dist_same: Vec<i64>,
    pub chain_id: Vec<u64>,
    pub chain_index: Vec<u32>,
    pub link_confidence: Vec<f32>,
    pub link_dt_ns: Vec<u64>,
}

impl Records {
    pub fn len(&self) -> usize {
        self.order_id.len()
    }
}

pub struct Tracker {
    cfg: LifecycleConfig,
    by_instrument: FxHashMap<u32, InstrumentTracker>,
    pub records: Records,
    digest: Fnv1a,
    /// links accepted / candidate slots registered (diagnostics)
    pub links_made: u64,
    pub refill_slots: u64,
    /// total records emitted (across drains)
    pub emitted: u64,
}

/// (best_bid, best_ask) prices as observed on the book.
pub type Bests = (Option<i64>, Option<i64>);

fn dist_same(side: u8, price: i64, bests: Bests) -> i64 {
    let best = if side == SIDE_BID { bests.0 } else { bests.1 };
    match best {
        // bids rest below the best bid; asks above the best ask
        Some(b) if side == SIDE_BID => b - price,
        Some(b) => price - b,
        None => DIST_SENTINEL,
    }
}

/// Closest same-side-best approach to `price` over one segment: the extreme
/// of the prevailing-at-start value and any post-start changes.
fn closest_approach(
    side: u8,
    price: i64,
    seg_start: u64,
    seg_prevail: Option<i64>,
    approach_bid: &ApproachSide,
    approach_ask: &ApproachSide,
) -> Option<i64> {
    if side == SIDE_BID {
        let after = approach_bid.extreme_since(seg_start);
        let best = match (seg_prevail, after) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        };
        best.map(|b| b - price)
    } else {
        let after = approach_ask.extreme_since(seg_start);
        let best = match (seg_prevail, after) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        };
        best.map(|b| price - b)
    }
}

fn dist_mid2(side: u8, price: i64, bests: Bests) -> i64 {
    match bests {
        (Some(bb), Some(ba)) => {
            // 2x (mid - price) for bids / 2x (price - mid) for asks, kept
            // integer (mid can be a half tick); >= 0 when inside the spread's
            // own side of mid.
            if side == SIDE_BID {
                (bb + ba) - 2 * price
            } else {
                2 * price - (bb + ba)
            }
        }
        _ => DIST_SENTINEL,
    }
}

impl Tracker {
    pub fn new(cfg: LifecycleConfig) -> Self {
        Tracker {
            cfg,
            by_instrument: FxHashMap::default(),
            records: Records::default(),
            digest: Fnv1a::new(),
            links_made: 0,
            refill_slots: 0,
            emitted: 0,
        }
    }

    pub fn lifecycle_digest(&self) -> u64 {
        self.digest.finish()
    }

    /// Track best-price movement for approach/survival measurement. Call
    /// after any event that may have moved a best price.
    pub fn on_bests(&mut self, iid: u32, ts: u64, bests: Bests) {
        let t = self.by_instrument.entry(iid).or_default();
        if let Some(bb) = bests.0 {
            if t.last_best_bid != Some(bb) {
                t.last_best_bid = Some(bb);
                t.approach_bid.push_min(ts, bb);
            }
        }
        if let Some(ba) = bests.1 {
            if t.last_best_ask != Some(ba) {
                t.last_best_ask = Some(ba);
                t.approach_ask.push_max(ts, ba);
            }
        }
    }

    /// New order entered the book (A, or unknown-M treated as add).
    /// `bests` is the post-insertion book state. Returns the chain link made
    /// for this order, if any (for tests/diagnostics).
    #[allow(clippy::too_many_arguments)]
    pub fn on_add(
        &mut self,
        iid: u32,
        order_id: u64,
        o: &Order,
        queue_pos_at_add: u32,
        vol_ahead_at_add: u64,
        bests: Bests,
        entered_unknown_modify: bool,
    ) {
        let window = self.cfg.iceberg_window_ns;
        let tol = self.cfg.iceberg_clip_tol;
        let t = self.by_instrument.entry(iid).or_default();

        // iceberg-refill linking (never for snapshot adds: a snapshot is a
        // book image, not a refill arrival)
        let mut chain = (0u64, 0u32, 0.0f32, 0u64);
        if !o.from_snapshot {
            let key = (o.side, o.price);
            if let Some(c) = t.refill.get(&key) {
                let dt = o.ts_added.saturating_sub(c.ts);
                if dt <= window && (o.initial_size as f64) <= tol * c.clip as f64 {
                    // heuristic confidence: exact-clip immediate refill -> 1.0;
                    // decays with latency, discounted on size mismatch
                    let time_factor = 1.0 - 0.5 * (dt as f64 / window as f64);
                    let size_factor = if o.initial_size == c.clip {
                        1.0
                    } else if o.initial_size < c.clip {
                        0.75
                    } else {
                        0.5 // > clip but within tolerance (tol > 1 configs)
                    };
                    chain = (
                        c.chain_id,
                        c.chain_index + 1,
                        (time_factor * size_factor) as f32,
                        dt,
                    );
                    self.links_made += 1;
                    t.refill.remove(&key); // one refill per exhaustion
                } else if dt > window {
                    t.refill.remove(&key); // stale slot
                }
            }
        }

        t.live.insert(
            order_id,
            Live {
                price_at_add: o.price,
                queue_pos_at_add,
                vol_ahead_at_add,
                dist_same_at_add: dist_same(o.side, o.price, bests),
                dist_mid2_at_add: dist_mid2(o.side, o.price, bests),
                seg_start_ts: o.ts_added,
                seg_prevail: if o.side == SIDE_BID { bests.0 } else { bests.1 },
                min_dist_same: MIN_DIST_SENTINEL,
                n_size_increases: 0,
                n_size_decreases: 0,
                n_price_changes: 0,
                cancelled_size: 0,
                max_size: o.initial_size,
                entered_unknown_modify,
                chain_id: chain.0,
                chain_index: chain.1,
                link_confidence: chain.2,
                link_dt_ns: chain.3,
            },
        );
    }

    /// Modify observed (known order). `fill_application` marks an M that
    /// applies a partial fill (same-event F) rather than a trader action.
    /// `bests` is the post-modify book state (new segment's market view).
    #[allow(clippy::too_many_arguments)]
    pub fn on_modify(
        &mut self,
        iid: u32,
        order_id: u64,
        side: u8,
        old_price: i64,
        new_price: i64,
        old_size: u32,
        new_size: u32,
        ts: u64,
        fill_application: bool,
        bests: Bests,
    ) {
        let Some(t) = self.by_instrument.get_mut(&iid) else {
            return;
        };
        let Some(l) = t.live.get_mut(&order_id) else {
            return;
        };
        if new_price != old_price {
            // close the price segment at the old level: fold in the closest
            // approach the same-side best made to it while the order sat there
            let approach = closest_approach(
                side,
                old_price,
                l.seg_start_ts,
                l.seg_prevail,
                &t.approach_bid,
                &t.approach_ask,
            );
            if let Some(d) = approach {
                l.min_dist_same = l.min_dist_same.min(d);
            }
            l.seg_start_ts = ts;
            l.seg_prevail = if side == SIDE_BID { bests.0 } else { bests.1 };
            l.n_price_changes += 1;
        }
        if new_size > old_size {
            l.n_size_increases += 1;
            l.max_size = l.max_size.max(new_size);
        } else if new_size < old_size {
            l.n_size_decreases += 1;
            if !fill_application {
                // voluntary reduction = cancelled quantity; a fill-applying M
                // reduction is execution, not cancellation
                l.cancelled_size += (old_size - new_size) as u64;
            }
        }
    }

    /// Order left the book. `o` is the order as removed; `bests` must be the
    /// book state BEFORE removal (the order still counted in the market).
    #[allow(clippy::too_many_arguments)]
    pub fn on_terminate(
        &mut self,
        iid: u32,
        order_id: u64,
        o: &Order,
        state: u8,
        queue_pos_at_term: u32,
        ts: u64,
        bests: Bests,
    ) {
        let t = self.by_instrument.entry(iid).or_default();
        let mut l = t.live.remove(&order_id).unwrap_or_else(|| Live {
            // order predates tracking (never happens after a clean open, but
            // keep the engine total: every removal emits exactly one record)
            price_at_add: o.price,
            queue_pos_at_add: POS_SENTINEL,
            vol_ahead_at_add: u64::MAX,
            dist_same_at_add: DIST_SENTINEL,
            dist_mid2_at_add: DIST_SENTINEL,
            seg_start_ts: o.ts_added,
            seg_prevail: None,
            min_dist_same: MIN_DIST_SENTINEL,
            n_size_increases: 0,
            n_size_decreases: 0,
            n_price_changes: 0,
            cancelled_size: 0,
            max_size: o.initial_size,
            entered_unknown_modify: false,
            chain_id: 0,
            chain_index: 0,
            link_confidence: 0.0,
            link_dt_ns: 0,
        });

        // close the final price segment
        let approach = closest_approach(
            o.side,
            o.price,
            l.seg_start_ts,
            l.seg_prevail,
            &t.approach_bid,
            &t.approach_ask,
        );
        if let Some(d) = approach {
            l.min_dist_same = l.min_dist_same.min(d);
        }

        // a pulled order that executed nothing vs. partially
        let state = if state == STATE_CANCELLED && o.filled_size > 0 {
            STATE_PARTIAL_CANCELLED
        } else {
            state
        };

        // fill-removal = displayed clip exhausted -> open an iceberg-refill
        // slot; the chain root is this order unless it is itself a refill
        if state == STATE_FILLED {
            let chain_id = if l.chain_id != 0 { l.chain_id } else { order_id };
            l.chain_id = chain_id;
            t.refill.insert(
                (o.side, o.price),
                RefillCandidate {
                    ts,
                    chain_id,
                    chain_index: l.chain_index,
                    clip: o.initial_size,
                },
            );
            self.refill_slots += 1;
        }

        let r = &mut self.records;
        r.instrument_id.push(iid);
        r.order_id.push(order_id);
        r.side.push(o.side);
        r.price_at_add.push(l.price_at_add);
        r.price_final.push(o.price);
        r.ts_added.push(o.ts_added);
        r.ts_terminated.push(ts);
        r.from_snapshot.push(o.from_snapshot as u8);
        r.entered_unknown_modify.push(l.entered_unknown_modify as u8);
        r.initial_size.push(o.initial_size);
        r.max_size.push(l.max_size.max(o.current_size));
        r.final_size.push(o.current_size);
        r.filled_size.push(o.filled_size);
        r.cancelled_size.push(match state {
            // pulled: the resting remainder is cancelled quantity too
            STATE_CANCELLED | STATE_PARTIAL_CANCELLED => {
                l.cancelled_size + o.current_size as u64
            }
            _ => l.cancelled_size,
        });
        r.n_size_increases.push(l.n_size_increases);
        r.n_size_decreases.push(l.n_size_decreases);
        r.n_price_changes.push(l.n_price_changes);
        r.final_state.push(state);
        r.queue_pos_at_add.push(l.queue_pos_at_add);
        r.vol_ahead_at_add.push(l.vol_ahead_at_add);
        r.queue_pos_at_term.push(queue_pos_at_term);
        r.dist_same_at_add.push(l.dist_same_at_add);
        r.dist_mid2_at_add.push(l.dist_mid2_at_add);
        r.dist_same_at_term.push(dist_same(o.side, o.price, bests));
        r.dist_mid2_at_term.push(dist_mid2(o.side, o.price, bests));
        r.min_dist_same.push(l.min_dist_same);
        r.chain_id.push(l.chain_id);
        r.chain_index.push(l.chain_index);
        r.link_confidence.push(l.link_confidence);
        r.link_dt_ns.push(l.link_dt_ns);

        self.emitted += 1;

        // running determinism digest over every emitted field
        let h = &mut self.digest;
        h.write_u64(iid as u64);
        h.write_u64(order_id);
        h.write_u64(o.side as u64);
        h.write_i64(l.price_at_add);
        h.write_i64(o.price);
        h.write_u64(o.ts_added);
        h.write_u64(ts);
        h.write_u64(state as u64);
        h.write_u64(o.filled_size as u64);
        h.write_u64(o.current_size as u64);
        h.write_u64(l.queue_pos_at_add as u64);
        h.write_u64(queue_pos_at_term as u64);
        h.write_i64(l.min_dist_same);
        h.write_u64(l.chain_id);
        h.write_u64(l.chain_index as u64);
        h.write_u64(l.link_dt_ns);
    }

    /// Book clear (R): all lifecycle state for the instrument resets; the
    /// engine emits Cleared records for the swept orders before calling this.
    pub fn on_clear(&mut self, iid: u32) {
        if let Some(t) = self.by_instrument.get_mut(&iid) {
            t.live.clear();
            t.approach_bid.clear();
            t.approach_ask.clear();
            t.last_best_bid = None;
            t.last_best_ask = None;
            t.refill.clear();
        }
    }

    /// Take all completed records, leaving the tracker running.
    pub fn drain(&mut self) -> Records {
        std::mem::take(&mut self.records)
    }
}
