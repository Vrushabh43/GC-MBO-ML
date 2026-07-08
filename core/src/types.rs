//! Shared types for the compiled core (plan Phase 0/1).

/// DBN sentinel for "no price" (i64::MAX).
pub const UNDEF_PRICE: i64 = i64::MAX;

// DBN flag bits (mirrors dbn::flags; re-declared so the engine's semantics
// are explicit and testable without the decoder).
pub const F_LAST: u8 = 1 << 7;
pub const F_SNAPSHOT: u8 = 1 << 5;

pub const SIDE_BID: u8 = b'B';
pub const SIDE_ASK: u8 = b'A';

/// Order lifecycle state (Phase 1 subset; Phase 2 extends analytics).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u8)]
pub enum OrderState {
    Active = 0,
    PartiallyFilled = 1,
}

/// A resting order tracked by the individual-order store (plan Phase 1).
///
/// GLBX MBO semantics (verified empirically on real data, 2026-07-08):
/// `F` (Fill) records are informational execution attribution — the book
/// mutation always arrives as an explicit follow-up record in the same
/// matching event (`C` after a full fill, `M` with the reduced size after a
/// partial fill). `current_size` therefore only changes via A/C/M/R;
/// fill volume accumulates in `filled_size` / `unapplied_fill`.
#[derive(Clone, Copy, Debug)]
pub struct Order {
    pub side: u8, // SIDE_BID | SIDE_ASK
    pub price: i64,
    pub current_size: u32,
    pub initial_size: u32,
    pub ts_added: u64,
    pub ts_last_updated: u64,
    pub state: OrderState,
    pub from_snapshot: bool,
    /// cumulative executed volume attributed to this order (analytics)
    pub filled_size: u32,
    /// fills not yet reflected by a follow-up C/M book mutation
    pub unapplied_fill: u32,
    /// ts_event of the most recent F record for this order
    pub last_fill_ts: u64,
}

/// Data-quality / invariant incident kinds (R1/R10).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u8)]
pub enum IncidentKind {
    DuplicateAdd = 0,
    UnknownCancel = 1,
    UnknownModify = 2,
    UnknownFill = 3,
    CancelSizeMismatch = 4,
    FillOverrun = 5,
    SequenceRegression = 6,
    TfReconcileMismatch = 7,
    CrossedBook = 8,
    StoreLevelsMismatch = 9,
}

impl IncidentKind {
    pub fn name(self) -> &'static str {
        match self {
            IncidentKind::DuplicateAdd => "duplicate_add",
            IncidentKind::UnknownCancel => "unknown_cancel",
            IncidentKind::UnknownModify => "unknown_modify",
            IncidentKind::UnknownFill => "unknown_fill",
            IncidentKind::CancelSizeMismatch => "cancel_size_mismatch",
            IncidentKind::FillOverrun => "fill_overrun",
            IncidentKind::SequenceRegression => "sequence_regression",
            IncidentKind::TfReconcileMismatch => "tf_reconcile_mismatch",
            IncidentKind::CrossedBook => "crossed_book",
            IncidentKind::StoreLevelsMismatch => "store_levels_mismatch",
        }
    }
}

/// A recorded incident (bounded ring; counters are always exact).
#[derive(Clone, Debug)]
pub struct Incident {
    pub kind: IncidentKind,
    pub ts_event: u64,
    pub instrument_id: u32,
    pub order_id: u64,
    pub sequence: u32,
    pub detail: String,
}

/// FNV-1a 64-bit — simple, dependency-free, fully deterministic.
#[derive(Clone, Copy)]
pub struct Fnv1a(pub u64);

impl Fnv1a {
    pub fn new() -> Self {
        Fnv1a(0xcbf2_9ce4_8422_2325)
    }
    #[inline]
    pub fn write_u64(&mut self, v: u64) {
        for b in v.to_le_bytes() {
            self.0 ^= b as u64;
            self.0 = self.0.wrapping_mul(0x0000_0100_0000_01B3);
        }
    }
    #[inline]
    pub fn write_i64(&mut self, v: i64) {
        self.write_u64(v as u64);
    }
    pub fn finish(self) -> u64 {
        self.0
    }
}

impl Default for Fnv1a {
    fn default() -> Self {
        Self::new()
    }
}
