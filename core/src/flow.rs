//! Per-matching-event-group flow PRIMITIVES for one tracked instrument
//! (plan Phase 3 support; Steps 7-12).
//!
//! Boundary (plan Phase 0): the compiled core emits FACTS about each
//! matching event — T-only volumes, add/pull flow at and near the touch,
//! per-level depth-flow (MLOFI ingredients), depth aggregates, lifecycle
//! terminations, iceberg refill links, best-level liquidity age — while all
//! FEATURES (rolling windows, ratios, composite scores) are composed in
//! Python by a streaming engine whose step() is the single code path for
//! historical replay and live (Critical Rule 3).
//!
//! One row is appended per matching-event group that touches the tracked
//! instrument, describing the group's flow and the post-group book state.
//! Rows drain to Python as raw little-endian column buffers, exactly like
//! the Phase 2 lifecycle records.
//!
//! Requires the Phase 2 lifecycle tracker (termination/refill fields).

use crate::book::InstrumentBook;
use crate::types::{SIDE_ASK, SIDE_BID};

pub struct FlowConfig {
    pub instrument_id: u32,
    /// one tick in fixed-point price units (GC: 0.1 pt = 100_000_000)
    pub tick: i64,
    /// "near the touch" band, in ticks, for add/pull flow
    pub near_ticks: i64,
    /// book levels tracked (plan: 10)
    pub levels: usize,
}

/// Termination summary forwarded from the lifecycle tracker (C-arm only).
pub struct TermSummary {
    pub filled: bool,
    pub touched: bool,
    pub is_refill_link: bool,
    pub lifetime_ns: u64,
}

const B: usize = 0;
const A: usize = 1;

#[inline]
fn si(side: u8) -> usize {
    if side == SIDE_BID {
        B
    } else {
        A
    }
}

/// Per-group accumulators (reset at every group flush).
#[derive(Default)]
struct GroupAcc {
    touched: bool,
    t_vol: [u64; 2],  // aggressor side: B = buy, A = sell
    t_n: [u32; 2],
    t_px_high: i64,
    t_px_low: i64,
    add_best: [u64; 2],
    add_near: [u64; 2],
    pull_best: [u64; 2],
    pull_near: [u64; 2],
    fill: [u64; 2], // resting side receiving fills
    hidden: [u64; 2],
    term_filled: u32,
    term_pulled_touched: u32,
    term_pulled_untouched: u32,
    term_filled_refill: u32,
    life_filled_sum: u64,
    life_pulled_sum: u64,
    term_filled_unchained: u32,
    life_filled_unchained_sum: u64,
    term_pulled_unchained: u32,
    life_pulled_unchained_sum: u64,
    refill: [u32; 2],
    refill_conf_sum: f32,
}

impl GroupAcc {
    fn reset(&mut self) {
        *self = GroupAcc {
            t_px_high: i64::MIN,
            t_px_low: i64::MAX,
            ..GroupAcc::default()
        };
    }
}

/// Drained columns, struct-of-vectors.
#[derive(Default)]
pub struct FlowColumns {
    pub ts: Vec<u64>,
    pub valid: Vec<u8>,
    pub bid_px: Vec<i64>,
    pub ask_px: Vec<i64>,
    pub bid_sz: Vec<u32>,
    pub ask_sz: Vec<u32>,
    pub bid_ct: Vec<u32>,
    pub ask_ct: Vec<u32>,
    pub depth_b_near: Vec<u32>,
    pub depth_b_mid: Vec<u32>,
    pub depth_b_deep: Vec<u32>,
    pub depth_a_near: Vec<u32>,
    pub depth_a_mid: Vec<u32>,
    pub depth_a_deep: Vec<u32>,
    /// per-level depth-flow (MLOFI ingredient), level index 0..levels-1
    pub flow_b: Vec<Vec<i32>>,
    pub flow_a: Vec<Vec<i32>>,
    pub t_buy: Vec<u32>,
    pub t_sell: Vec<u32>,
    pub t_buy_n: Vec<u32>,
    pub t_sell_n: Vec<u32>,
    pub t_px_high: Vec<i64>,
    pub t_px_low: Vec<i64>,
    pub add_best_b: Vec<u32>,
    pub add_near_b: Vec<u32>,
    pub pull_best_b: Vec<u32>,
    pub pull_near_b: Vec<u32>,
    pub add_best_a: Vec<u32>,
    pub add_near_a: Vec<u32>,
    pub pull_best_a: Vec<u32>,
    pub pull_near_a: Vec<u32>,
    pub fill_b: Vec<u32>,
    pub fill_a: Vec<u32>,
    pub hidden_b: Vec<u32>,
    pub hidden_a: Vec<u32>,
    pub term_filled: Vec<u32>,
    pub term_pulled_touched: Vec<u32>,
    pub term_pulled_untouched: Vec<u32>,
    pub term_filled_refill: Vec<u32>,
    pub life_filled_sum: Vec<u64>,
    pub life_pulled_sum: Vec<u64>,
    pub term_filled_unchained: Vec<u32>,
    pub life_filled_unchained_sum: Vec<u64>,
    pub term_pulled_unchained: Vec<u32>,
    pub life_pulled_unchained_sum: Vec<u64>,
    pub refill_b: Vec<u32>,
    pub refill_a: Vec<u32>,
    pub refill_conf_sum: Vec<f32>,
    pub age_best_b: Vec<u64>,
    pub age_best_a: Vec<u64>,
}

impl FlowColumns {
    fn with_levels(levels: usize) -> Self {
        FlowColumns {
            flow_b: (0..levels).map(|_| Vec::new()).collect(),
            flow_a: (0..levels).map(|_| Vec::new()).collect(),
            ..FlowColumns::default()
        }
    }

    pub fn len(&self) -> usize {
        self.ts.len()
    }
}

pub struct FlowRecorder {
    pub cfg: FlowConfig,
    g: GroupAcc,
    /// previous emission's top-N (px, sz) per side, for depth-flow
    prev_top: [Vec<(i64, u64)>; 2],
    pub cols: FlowColumns,
    pub groups_emitted: u64,
}

impl FlowRecorder {
    pub fn new(cfg: FlowConfig) -> Self {
        let levels = cfg.levels;
        let mut g = GroupAcc::default();
        g.reset();
        FlowRecorder {
            cfg,
            g,
            prev_top: [Vec::new(), Vec::new()],
            cols: FlowColumns::with_levels(levels),
            groups_emitted: 0,
        }
    }

    #[inline]
    pub fn instrument(&self) -> u32 {
        self.cfg.instrument_id
    }

    /// Aggressor-side trade (T action). side is the AGGRESSOR side.
    #[inline]
    pub fn on_trade(&mut self, side: u8, size: u32, price: i64) {
        self.g.touched = true;
        self.g.t_vol[si(side)] += size as u64;
        self.g.t_n[si(side)] += 1;
        self.g.t_px_high = self.g.t_px_high.max(price);
        self.g.t_px_low = self.g.t_px_low.min(price);
    }

    /// New displayed liquidity. `dist_px` = same-side-best minus order price
    /// (post-insertion, >= 0 in an uncrossed book), in price units.
    #[inline]
    pub fn on_add(&mut self, side: u8, size: u32, dist_px: Option<i64>) {
        self.g.touched = true;
        if let Some(d) = dist_px {
            let s = si(side);
            if d <= 0 {
                self.g.add_best[s] += size as u64;
            }
            if d <= self.cfg.near_ticks * self.cfg.tick {
                self.g.add_near[s] += size as u64;
            }
        }
    }

    /// Trader pull (never fill-removal). `dist_px` measured pre-removal.
    #[inline]
    pub fn on_pull(&mut self, side: u8, size: u32, dist_px: Option<i64>) {
        self.g.touched = true;
        if let Some(d) = dist_px {
            let s = si(side);
            if d <= 0 {
                self.g.pull_best[s] += size as u64;
            }
            if d <= self.cfg.near_ticks * self.cfg.tick {
                self.g.pull_near[s] += size as u64;
            }
        }
    }

    /// Fill against a resting order; side is the RESTING side. `hidden` is
    /// the executed quantity beyond the displayed size (iceberg signature).
    #[inline]
    pub fn on_fill(&mut self, side: u8, size: u32, hidden: u32) {
        self.g.touched = true;
        self.g.fill[si(side)] += size as u64;
        self.g.hidden[si(side)] += hidden as u64;
    }

    /// Order termination (C arm only: fill-removal or trader pull).
    #[inline]
    pub fn on_termination(&mut self, t: &TermSummary) {
        self.g.touched = true;
        if t.filled {
            self.g.term_filled += 1;
            self.g.life_filled_sum += t.lifetime_ns;
            if t.is_refill_link {
                self.g.term_filled_refill += 1;
            } else {
                self.g.term_filled_unchained += 1;
                self.g.life_filled_unchained_sum += t.lifetime_ns;
            }
        } else {
            if t.touched {
                self.g.term_pulled_touched += 1;
            } else {
                self.g.term_pulled_untouched += 1;
            }
            self.g.life_pulled_sum += t.lifetime_ns;
            if !t.is_refill_link {
                self.g.term_pulled_unchained += 1;
                self.g.life_pulled_unchained_sum += t.lifetime_ns;
            }
        }
    }

    /// Iceberg refill link accepted for an add on `side`.
    #[inline]
    pub fn on_refill(&mut self, side: u8, confidence: f32) {
        self.g.touched = true;
        self.g.refill[si(side)] += 1;
        self.g.refill_conf_sum += confidence;
    }

    /// Book clear: previous-top state is no longer comparable.
    pub fn on_clear(&mut self) {
        self.prev_top[B].clear();
        self.prev_top[A].clear();
        self.g.reset();
    }

    /// Group boundary: emit one row if the group touched the instrument.
    pub fn flush(&mut self, ts: u64, book: Option<&InstrumentBook>) {
        if !self.g.touched {
            return;
        }
        let n = self.cfg.levels;
        let (top_b, top_a, bb, ba, age_b, age_a) = match book {
            Some(b) => {
                let tb = b.top_levels(SIDE_BID, n);
                let ta = b.top_levels(SIDE_ASK, n);
                let bb = tb.first().map(|&(p, s, c)| (p, s, c));
                let ba = ta.first().map(|&(p, s, c)| (p, s, c));
                let age = |side: u8, best: Option<(i64, u64, u32)>| -> u64 {
                    let Some((px, _, _)) = best else { return 0 };
                    let ages = b.level_ages(side, px, ts);
                    let mut wsum: u128 = 0;
                    let mut w: u128 = 0;
                    for (_, age_ns, sz, _) in ages {
                        wsum += age_ns as u128 * sz as u128;
                        w += sz as u128;
                    }
                    if w > 0 {
                        (wsum / w) as u64
                    } else {
                        0
                    }
                };
                let (age_b, age_a) = (age(SIDE_BID, bb), age(SIDE_ASK, ba));
                (tb, ta, bb, ba, age_b, age_a)
            }
            None => (Vec::new(), Vec::new(), None, None, 0, 0),
        };

        // per-level depth-flow vs previous emission (MLOFI ingredient):
        // improvement contributes +size, retreat contributes -prev_size,
        // same price contributes the size change. Bids improve upward,
        // asks improve downward. First emission after start/clear: zeros.
        for (s, top, better) in [
            (B, &top_b, 1i64),  // bid improved if px_now > px_prev
            (A, &top_a, -1i64), // ask improved if px_now < px_prev
        ] {
            let prev = &self.prev_top[s];
            let dst = if s == B {
                &mut self.cols.flow_b
            } else {
                &mut self.cols.flow_a
            };
            for l in 0..n {
                let now = top.get(l).map(|&(p, sz, _)| (p, sz));
                let old = prev.get(l).copied();
                let v: i64 = if prev.is_empty() {
                    0
                } else {
                    match (now, old) {
                        (Some((pn, szn)), Some((po, szo))) => {
                            if (pn - po) * better > 0 {
                                szn as i64
                            } else if (pn - po) * better < 0 {
                                -(szo as i64)
                            } else {
                                szn as i64 - szo as i64
                            }
                        }
                        (Some((_, szn)), None) => szn as i64,
                        (None, Some((_, szo))) => -(szo as i64),
                        (None, None) => 0,
                    }
                };
                dst[l].push(v.clamp(i32::MIN as i64, i32::MAX as i64) as i32);
            }
        }
        self.prev_top[B] = top_b.iter().map(|&(p, s, _)| (p, s)).collect();
        self.prev_top[A] = top_a.iter().map(|&(p, s, _)| (p, s)).collect();

        let sum_rng = |top: &[(i64, u64, u32)], lo: usize, hi: usize| -> u32 {
            top.iter()
                .skip(lo)
                .take(hi - lo)
                .map(|&(_, s, _)| s)
                .sum::<u64>()
                .min(u32::MAX as u64) as u32
        };

        let c = &mut self.cols;
        let g = &self.g;
        c.ts.push(ts);
        c.valid.push((bb.is_some() && ba.is_some()) as u8);
        c.bid_px.push(bb.map(|x| x.0).unwrap_or(0));
        c.ask_px.push(ba.map(|x| x.0).unwrap_or(0));
        c.bid_sz.push(bb.map(|x| x.1.min(u32::MAX as u64) as u32).unwrap_or(0));
        c.ask_sz.push(ba.map(|x| x.1.min(u32::MAX as u64) as u32).unwrap_or(0));
        c.bid_ct.push(bb.map(|x| x.2).unwrap_or(0));
        c.ask_ct.push(ba.map(|x| x.2).unwrap_or(0));
        c.depth_b_near.push(sum_rng(&top_b, 0, 3));
        c.depth_b_mid.push(sum_rng(&top_b, 3, 6));
        c.depth_b_deep.push(sum_rng(&top_b, 6, n));
        c.depth_a_near.push(sum_rng(&top_a, 0, 3));
        c.depth_a_mid.push(sum_rng(&top_a, 3, 6));
        c.depth_a_deep.push(sum_rng(&top_a, 6, n));
        c.t_buy.push(g.t_vol[B].min(u32::MAX as u64) as u32);
        c.t_sell.push(g.t_vol[A].min(u32::MAX as u64) as u32);
        c.t_buy_n.push(g.t_n[B]);
        c.t_sell_n.push(g.t_n[A]);
        c.t_px_high.push(if g.t_px_high == i64::MIN { 0 } else { g.t_px_high });
        c.t_px_low.push(if g.t_px_low == i64::MAX { 0 } else { g.t_px_low });
        c.add_best_b.push(g.add_best[B].min(u32::MAX as u64) as u32);
        c.add_near_b.push(g.add_near[B].min(u32::MAX as u64) as u32);
        c.pull_best_b.push(g.pull_best[B].min(u32::MAX as u64) as u32);
        c.pull_near_b.push(g.pull_near[B].min(u32::MAX as u64) as u32);
        c.add_best_a.push(g.add_best[A].min(u32::MAX as u64) as u32);
        c.add_near_a.push(g.add_near[A].min(u32::MAX as u64) as u32);
        c.pull_best_a.push(g.pull_best[A].min(u32::MAX as u64) as u32);
        c.pull_near_a.push(g.pull_near[A].min(u32::MAX as u64) as u32);
        c.fill_b.push(g.fill[B].min(u32::MAX as u64) as u32);
        c.fill_a.push(g.fill[A].min(u32::MAX as u64) as u32);
        c.hidden_b.push(g.hidden[B].min(u32::MAX as u64) as u32);
        c.hidden_a.push(g.hidden[A].min(u32::MAX as u64) as u32);
        c.term_filled.push(g.term_filled);
        c.term_pulled_touched.push(g.term_pulled_touched);
        c.term_pulled_untouched.push(g.term_pulled_untouched);
        c.term_filled_refill.push(g.term_filled_refill);
        c.life_filled_sum.push(g.life_filled_sum);
        c.life_pulled_sum.push(g.life_pulled_sum);
        c.term_filled_unchained.push(g.term_filled_unchained);
        c.life_filled_unchained_sum.push(g.life_filled_unchained_sum);
        c.term_pulled_unchained.push(g.term_pulled_unchained);
        c.life_pulled_unchained_sum.push(g.life_pulled_unchained_sum);
        c.refill_b.push(g.refill[B]);
        c.refill_a.push(g.refill[A]);
        c.refill_conf_sum.push(g.refill_conf_sum);
        c.age_best_b.push(age_b);
        c.age_best_a.push(age_a);

        self.groups_emitted += 1;
        self.g.reset();
    }

    pub fn drain(&mut self) -> FlowColumns {
        std::mem::replace(&mut self.cols, FlowColumns::with_levels(self.cfg.levels))
    }
}
