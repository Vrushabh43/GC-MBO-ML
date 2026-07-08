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
    fn add(&mut self, price: i64, order_id: u64, size: u32) {
        let lvl = self.levels.entry(price).or_default();
        lvl.total_size += size as u64;
        lvl.order_count += 1;
        lvl.fifo.push_back(order_id);
    }

    fn remove(&mut self, price: i64, order_id: u64, size: u32) {
        if let Some(lvl) = self.levels.get_mut(&price) {
            lvl.total_size = lvl.total_size.saturating_sub(size as u64);
            lvl.order_count = lvl.order_count.saturating_sub(1);
            if let Some(pos) = lvl.fifo.iter().position(|&id| id == order_id) {
                lvl.fifo.remove(pos);
            }
            if lvl.order_count == 0 {
                self.levels.remove(&price);
            }
        }
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

    /// Insert a new order. Returns false if the order id already existed
    /// (caller logs the incident; the stale order is replaced to keep the
    /// two views synchronized).
    pub fn add_order(&mut self, order_id: u64, order: Order) -> bool {
        if let Some(old) = self.orders.remove(&order_id) {
            let (p, s) = (old.price, old.current_size);
            self.side_mut(old.side).remove(p, order_id, s);
            self.orders.insert(order_id, order);
            self.side_mut(order.side).add(order.price, order_id, order.current_size);
            return false;
        }
        self.orders.insert(order_id, order);
        self.side_mut(order.side).add(order.price, order_id, order.current_size);
        true
    }

    /// Full cancel (book removal). Returns the removed order so the caller
    /// can classify it (trader pull vs fill-removal) and verify sizes.
    pub fn cancel_order(&mut self, order_id: u64) -> Option<Order> {
        let o = self.orders.remove(&order_id)?;
        self.side_mut(o.side).remove(o.price, order_id, o.current_size);
        Some(o)
    }

    /// Modify price and/or size per Globex priority rules.
    /// Returns false if the order is unknown.
    pub fn modify_order(
        &mut self,
        order_id: u64,
        new_price: i64,
        new_size: u32,
        ts: u64,
    ) -> bool {
        let Some(o) = self.orders.get_mut(&order_id) else {
            return false;
        };
        let (side, old_price, old_size) = (o.side, o.price, o.current_size);
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
        true
    }

    /// Record an execution against a resting order. Per verified GLBX
    /// semantics this NEVER mutates the book — the explicit follow-up C/M
    /// record performs the removal/reduction. Returns
    /// (found, unapplied_fill_exceeds_size).
    pub fn record_fill(&mut self, order_id: u64, fill_size: u32, ts: u64) -> (bool, bool) {
        let Some(o) = self.orders.get_mut(&order_id) else {
            return (false, false);
        };
        o.filled_size = o.filled_size.saturating_add(fill_size);
        o.unapplied_fill = o.unapplied_fill.saturating_add(fill_size);
        o.last_fill_ts = ts;
        o.state = OrderState::PartiallyFilled;
        (true, o.unapplied_fill > o.current_size)
    }

    pub fn best_bid(&self) -> Option<(i64, &Level)> {
        self.bids.levels.iter().next_back().map(|(p, l)| (*p, l))
    }

    pub fn best_ask(&self) -> Option<(i64, &Level)> {
        self.asks.levels.iter().next().map(|(p, l)| (*p, l))
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
