//! Price-level book + per-instrument order store (plan Phase 1, Steps 3-4).
//!
//! Two synchronized views per instrument:
//!   - individual-order store: order_id -> Order
//!   - aggregated price-level book: BTreeMap<price, Level> per side, with a
//!     FIFO of order ids per level (queue_position source for Phase 2).
//!
//! Determinism: BTreeMap iteration for all ordered outputs; integer-only
//! aggregation; the order-store digest uses an order-independent XOR fold.

use std::collections::{BTreeMap, VecDeque};

use rustc_hash::FxHashMap;

use crate::types::{Fnv1a, Order, OrderState, SIDE_ASK, SIDE_BID};

#[derive(Default)]
pub struct Level {
    pub total_size: u64,
    pub order_count: u32,
    pub fifo: VecDeque<u64>,
}

#[derive(Default)]
pub struct BookSide {
    pub levels: BTreeMap<i64, Level>,
}

impl BookSide {
    /// Insert at the back of the level's FIFO. Returns the queue state the
    /// order joined behind: (orders ahead, volume ahead) — the Phase 2
    /// queue-position-at-add measurement.
    fn add(&mut self, price: i64, order_id: u64, size: u32) -> (u32, u64) {
        let lvl = self.levels.entry(price).or_default();
        let ahead = (lvl.order_count, lvl.total_size);
        lvl.total_size += size as u64;
        lvl.order_count += 1;
        lvl.fifo.push_back(order_id);
        ahead
    }

    /// Remove an order from its level. Returns its FIFO position at removal
    /// (0 = front of queue) — the Phase 2 queue-position-at-termination.
    fn remove(&mut self, price: i64, order_id: u64, size: u32) -> Option<usize> {
        let mut removed_pos = None;
        if let Some(lvl) = self.levels.get_mut(&price) {
            lvl.total_size = lvl.total_size.saturating_sub(size as u64);
            lvl.order_count = lvl.order_count.saturating_sub(1);
            if let Some(pos) = lvl.fifo.iter().position(|&id| id == order_id) {
                lvl.fifo.remove(pos);
                removed_pos = Some(pos);
            }
            if lvl.order_count == 0 {
                self.levels.remove(&price);
            }
        }
        removed_pos
    }

    /// Size reduction in place (priority retained).
    fn reduce(&mut self, price: i64, delta: u64) {
        if let Some(lvl) = self.levels.get_mut(&price) {
            lvl.total_size = lvl.total_size.saturating_sub(delta);
        }
    }

    /// Size increase in place with FIFO priority loss (Globex rule).
    fn increase_lose_priority(&mut self, price: i64, order_id: u64, delta: u64) {
        if let Some(lvl) = self.levels.get_mut(&price) {
            lvl.total_size += delta;
            if let Some(pos) = lvl.fifo.iter().position(|&id| id == order_id) {
                lvl.fifo.remove(pos);
                lvl.fifo.push_back(order_id);
            }
        }
    }
}

/// Result of inserting an order (see `InstrumentBook::add_order`).
pub struct AddOutcome {
    pub replaced: Option<(Order, Option<usize>)>,
    pub queue_pos: u32,
    pub vol_ahead: u64,
}

#[derive(Default)]
pub struct InstrumentBook {
    pub orders: FxHashMap<u64, Order>,
    pub bids: BookSide,
    pub asks: BookSide,
}

impl InstrumentBook {
    fn side_mut(&mut self, side: u8) -> &mut BookSide {
        if side == SIDE_BID {
            &mut self.bids
        } else {
            &mut self.asks
        }
    }

    pub fn clear(&mut self) {
        self.orders.clear();
        self.bids.levels.clear();
        self.asks.levels.clear();
    }

    /// Insert a new order. `replaced` is Some (with the displaced order and
    /// its FIFO position) if the order id already existed — the caller logs
    /// the incident; the stale order is replaced to keep the two views
    /// synchronized. `queue_pos`/`vol_ahead` describe the queue the new
    /// order joined behind (Phase 2).
    pub fn add_order(&mut self, order_id: u64, order: Order) -> AddOutcome {
        let mut replaced = None;
        if let Some(old) = self.orders.remove(&order_id) {
            let (p, s) = (old.price, old.current_size);
            let pos = self.side_mut(old.side).remove(p, order_id, s);
            replaced = Some((old, pos));
        }
        self.orders.insert(order_id, order);
        let (queue_pos, vol_ahead) =
            self.side_mut(order.side)
                .add(order.price, order_id, order.current_size);
        AddOutcome {
            replaced,
            queue_pos,
            vol_ahead,
        }
    }

    /// Full cancel (book removal). Returns the removed order — so the caller
    /// can classify it (trader pull vs fill-removal) and verify sizes — plus
    /// its FIFO position at removal (Phase 2).
    pub fn cancel_order(&mut self, order_id: u64) -> Option<(Order, Option<usize>)> {
        let o = self.orders.remove(&order_id)?;
        let pos = self.side_mut(o.side).remove(o.price, order_id, o.current_size);
        Some((o, pos))
    }

    /// Modify price and/or size per Globex priority rules.
    /// Returns the pre-modify (side, price, size, last_fill_ts) — the
    /// Phase 2 tracker classifies the change from these — or None if unknown.
    pub fn modify_order(
        &mut self,
        order_id: u64,
        new_price: i64,
        new_size: u32,
        ts: u64,
    ) -> Option<(u8, i64, u32, u64)> {
        let Some(o) = self.orders.get_mut(&order_id) else {
            return None;
        };
        let (side, old_price, old_size) = (o.side, o.price, o.current_size);
        let last_fill_ts = o.last_fill_ts;
        o.price = new_price;
        o.current_size = new_size;
        o.ts_last_updated = ts;
        o.unapplied_fill = 0; // book mutation reflects any pending fills
        if new_price != old_price {
            // price change: leave old level, join back of new level's queue
            self.side_mut(side).remove(old_price, order_id, old_size);
            self.side_mut(side).add(new_price, order_id, new_size);
        } else if new_size > old_size {
            // size increase: priority lost
            self.side_mut(side)
                .increase_lose_priority(old_price, order_id, (new_size - old_size) as u64);
        } else if new_size < old_size {
            // size decrease: priority retained
            self.side_mut(side).reduce(old_price, (old_size - new_size) as u64);
        }
        Some((side, old_price, old_size, last_fill_ts))
    }

    /// Record an execution against a resting order. Per verified GLBX
    /// semantics this NEVER mutates the book — the explicit follow-up C/M
    /// record performs the removal/reduction. Returns
    /// (found, unapplied_fill_exceeds_size, hidden_quantity) where hidden
    /// is the single-fill excess over the displayed size (iceberg
    /// execution signature, a Phase 3 flow primitive).
    pub fn record_fill(&mut self, order_id: u64, fill_size: u32, ts: u64) -> (bool, bool, u32) {
        let Some(o) = self.orders.get_mut(&order_id) else {
            return (false, false, 0);
        };
        let hidden = fill_size.saturating_sub(o.current_size);
        o.filled_size = o.filled_size.saturating_add(fill_size);
        o.unapplied_fill = o.unapplied_fill.saturating_add(fill_size);
        o.last_fill_ts = ts;
        o.state = OrderState::PartiallyFilled;
        (true, o.unapplied_fill > o.current_size, hidden)
    }

    pub fn best_bid(&self) -> Option<(i64, &Level)> {
        self.bids.levels.iter().next_back().map(|(p, l)| (*p, l))
    }

    pub fn best_ask(&self) -> Option<(i64, &Level)> {
        self.asks.levels.iter().next().map(|(p, l)| (*p, l))
    }

    /// (best bid price, best ask price) — the Phase 2 tracker's market view.
    pub fn best_prices(&self) -> (Option<i64>, Option<i64>) {
        (
            self.bids.levels.keys().next_back().copied(),
            self.asks.levels.keys().next().copied(),
        )
    }

    /// Volume resting ahead of an order in its level's FIFO (Phase 2 queue
    /// query; queue position itself is the FIFO index).
    pub fn volume_ahead(&self, order_id: u64) -> Option<u64> {
        let o = self.orders.get(&order_id)?;
        let side = if o.side == SIDE_BID { &self.bids } else { &self.asks };
        let fifo = &side.levels.get(&o.price)?.fifo;
        let mut vol = 0u64;
        for &id in fifo.iter() {
            if id == order_id {
                return Some(vol);
            }
            vol += self.orders.get(&id).map(|x| x.current_size as u64).unwrap_or(0);
        }
        None
    }

    /// Liquidity-age distribution of a price level, front of queue first:
    /// (order_id, age_ns relative to `ts_now`, current_size, from_snapshot).
    /// Phase 3 liquidity-age features sample this.
    pub fn level_ages(
        &self,
        side: u8,
        price: i64,
        ts_now: u64,
    ) -> Vec<(u64, u64, u32, bool)> {
        let s = if side == SIDE_BID { &self.bids } else { &self.asks };
        let Some(lvl) = s.levels.get(&price) else {
            return Vec::new();
        };
        lvl.fifo
            .iter()
            .filter_map(|&id| {
                self.orders.get(&id).map(|o| {
                    (
                        id,
                        ts_now.saturating_sub(o.ts_added),
                        o.current_size,
                        o.from_snapshot,
                    )
                })
            })
            .collect()
    }

    /// Top-n levels from the aggregated price-level view.
    /// Returns (price, total_size, order_count) best-first.
    pub fn top_levels(&self, side: u8, n: usize) -> Vec<(i64, u64, u32)> {
        let take = |it: &mut dyn Iterator<Item = (&i64, &Level)>| {
            it.take(n)
                .map(|(p, l)| (*p, l.total_size, l.order_count))
                .collect::<Vec<_>>()
        };
        if side == SIDE_BID {
            take(&mut self.bids.levels.iter().rev())
        } else {
            take(&mut self.asks.levels.iter())
        }
    }

    /// Top-n levels recomputed from the individual-order store (Milestone 1
    /// requires both views). Deterministic: aggregation into a BTreeMap.
    pub fn top_levels_from_orders(&self, side: u8, n: usize) -> Vec<(i64, u64, u32)> {
        let mut agg: BTreeMap<i64, (u64, u32)> = BTreeMap::new();
        for o in self.orders.values() {
            if o.side == side {
                let e = agg.entry(o.price).or_insert((0, 0));
                e.0 += o.current_size as u64;
                e.1 += 1;
            }
        }
        let take = |it: &mut dyn Iterator<Item = (&i64, &(u64, u32))>| {
            it.take(n)
                .map(|(p, (s, c))| (*p, *s, *c))
                .collect::<Vec<_>>()
        };
        if side == SIDE_BID {
            take(&mut agg.iter().rev())
        } else {
            take(&mut agg.iter())
        }
    }

    /// Full cross-view consistency check (R1 invariant: order-store totals ==
    /// level aggregates, per side, every level). Returns first mismatch.
    pub fn views_consistent(&self) -> Result<(), String> {
        for (side, book_side) in [(SIDE_BID, &self.bids), (SIDE_ASK, &self.asks)] {
            let mut agg: BTreeMap<i64, (u64, u32)> = BTreeMap::new();
            for o in self.orders.values() {
                if o.side == side {
                    let e = agg.entry(o.price).or_insert((0, 0));
                    e.0 += o.current_size as u64;
                    e.1 += 1;
                }
            }
            if agg.len() != book_side.levels.len() {
                return Err(format!(
                    "side {}: level count orders={} levels={}",
                    side as char,
                    agg.len(),
                    book_side.levels.len()
                ));
            }
            for ((pa, (sa, ca)), (pl, lvl)) in agg.iter().zip(book_side.levels.iter()) {
                if pa != pl || *sa != lvl.total_size || *ca != lvl.order_count {
                    return Err(format!(
                        "side {}: order-view ({},{},{}) != level-view ({},{},{})",
                        side as char, pa, sa, ca, pl, lvl.total_size, lvl.order_count
                    ));
                }
            }
        }
        Ok(())
    }

    /// Deterministic digest of full book state (levels ordered; order store
    /// folded order-independently).
    pub fn digest(&self, h: &mut Fnv1a) {
        for (side_tag, side) in [(0u64, &self.bids), (1u64, &self.asks)] {
            h.write_u64(side_tag);
            for (p, l) in side.levels.iter() {
                h.write_i64(*p);
                h.write_u64(l.total_size);
                h.write_u64(l.order_count as u64);
            }
        }
        // order-independent fold over the hash map
        let mut fold: u64 = 0;
        for (id, o) in self.orders.iter() {
            let mut oh = Fnv1a::new();
            oh.write_u64(*id);
            oh.write_u64(o.side as u64);
            oh.write_i64(o.price);
            oh.write_u64(o.current_size as u64);
            oh.write_u64(o.initial_size as u64);
            oh.write_u64(o.filled_size as u64);
            oh.write_u64(o.ts_added);
            fold ^= oh.finish();
        }
        h.write_u64(fold);
        h.write_u64(self.orders.len() as u64);
    }
}
