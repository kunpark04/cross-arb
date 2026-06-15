use crate::config::Config;
use crate::exec::{self, ExecutionBackend};
use crate::pair::{lock, FlatKind, LivePair, SubmitKind, SubmitOutcome};
use crate::pricing::{flatten_exit_cents, unwind_exit_cents};
use crate::risk::Exposure;
use crate::types::*;
use crate::{book, discovery, postpone, unwind};

/// RESERVE exposure for an entry the moment it is SPAWNED (not when it acks), so two concurrent in-flight
/// entries can't both size against the same free room and over-allocate (C5). The outcome arm then either
/// keeps the reservation (records the position on a both-filled fill) or releases it (any other outcome).
pub(crate) fn reserve_exposure(exposure: &mut Exposure, pos: &Position, cost_per: f64) {
    let notional = cost_per * pos.size as f64;
    *exposure.per_pair.entry(pos.market.clone()).or_insert(0.0) += notional;
    *exposure.per_cluster.entry(pos.cluster.clone()).or_insert(0.0) += notional;
    exposure.total += notional;
    exposure.open_positions += 1;
}

/// SUBTRACT the EXACT reservation `reserve_exposure` made for ONE position (`cost_per * size`, saturating at
/// 0), decrement `open_positions` by 1. This is the SINGLE inverse of `reserve_exposure` — design §1.3 / R1:
/// it UNIFIES the old `release_exposure` (an entry that didn't fully fill) and `decrement_exposure` (a tracked
/// position closing on unwind) so the two paths CANNOT drift. Both pass the SAME (pos, cost_per) the
/// reservation used, so the subtraction is exact whether the slug has one stacked position or several — it
/// removes only THIS position's contribution, never the whole per-pair bucket. The per-pair bucket reaches
/// ~0 only when the LAST position on the slug is subtracted (the caller drops the slug key then).
pub(crate) fn subtract_exposure(exposure: &mut Exposure, pos: &Position, cost_per: f64) {
    let notional = cost_per * pos.size as f64;
    if let Some(p) = exposure.per_pair.get_mut(&pos.market) {
        *p = (*p - notional).max(0.0);
    }
    if let Some(c) = exposure.per_cluster.get_mut(&pos.cluster) {
        *c = (*c - notional).max(0.0);
    }
    exposure.total = (exposure.total - notional).max(0.0);
    exposure.open_positions = exposure.open_positions.saturating_sub(1);
}

/// APPEND a freshly-filled position as a new `HeldLeg` on its slug (design §1). The exposure was already
/// RESERVED at spawn (`reserve_exposure`), so this does NOT touch exposure — it only records the leg +
/// stores its `cost_per`/`entry_net`/`entry_dir` (so a later per-leg unwind subtracts EXACTLY this leg — R1 —
/// and a later add gates on `max(entry_net)+tau` / same-direction). Metadata is set on the FIRST insert
/// (Vacant) only; an APPEND (Occupied) pushes the leg and leaves the slug-level metadata AND `prev` UNTOUCHED
/// — so the C6 "preserve the poll's accumulated `prev`" concern is now STRUCTURAL (prev is a slug property,
/// never re-derived per leg). For a SPORTS pair the poll needs league/date/abbrevs.
pub(crate) fn track_position(
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    pair: &LivePair,
    pos: Position,
    cost_per: f64,
    entry_net: f64,
    entry_dir: Dir,
) {
    let leg = postpone::HeldLeg { pos, cost_per, entry_net, entry_dir };
    let slug = leg.pos.market.clone();
    use std::collections::hash_map::Entry;
    match lock(positions).entry(slug) {
        // APPEND: prev + metadata are slug-level (one game) and shared by every add -> never touched here.
        Entry::Occupied(mut e) => {
            e.get_mut().legs.push(leg);
        }
        Entry::Vacant(e) => {
            // poll metadata is for the MLB postponement poll ONLY. A WORLD-CUP pair is `Cat::Sports` but has
            // no statsapi source (`soccer`), so it enrolls with EMPTY metadata (like weather/econ) -> the poll
            // skips it. Its tiny void/postpone tail is the noted follow-on, not handled by the MLB poll.
            let (league, date, team_a, team_b) = if pair.cat == Cat::Sports && !pair.soccer {
                let last_seg = |t: &str| t.rsplit('-').next().unwrap_or("").to_ascii_lowercase();
                (
                    discovery::pm_league(&pair.slug).unwrap_or_default(),
                    discovery::iso_date(&pair.slug).unwrap_or_default(),
                    last_seg(&pair.kalshi),
                    pair.kalshi_b.as_deref().map(last_seg).unwrap_or_default(),
                )
            } else {
                (String::new(), String::new(), String::new(), String::new())
            };
            e.insert(postpone::SlugPositions { league, date, team_a, team_b, prev: None, legs: vec![leg] });
        }
    }
}

/// HELD-SLUG ADD GATE (design §2/§3) — decide whether an approvable same-frame edge on an ALREADY-HELD slug
/// qualifies as an add, and if so which kind. Returns `Some("scale-in"|"re-entry")` to ALLOW (the caller then
/// runs `evaluate`, whose notional/count caps bound it) or `None` to BLOCK (== the old one-position guard's
/// `continue`). Gate — ALL must hold: (1) SAME-direction as every held leg (an opposite-direction bigger arb
/// is NOT an add — R6/Test 4); (2) `edge.net >= max(held entry_net) + add_tau_gain` (parity with the backtest
/// `add_events` trigger); (3) `held.len() < max_positions_per_slug` (the explicit count cap — cap=1 blocks the
/// first add); (4) the matching feature FLAG is ON (`was_live` => SCALE-IN needs `enable_scale_in`; else
/// RE-ENTRY needs `enable_reentry`).
///
/// SAFE BY DEFAULT: with both flags false AND cap=1, clause (3) (cap) and clause (4) (flag) BOTH fail, so this
/// ALWAYS returns `None` for a held slug — byte-identical to the one-position-per-slug bot.
pub(crate) fn qualifying_add(cfg: &Config, held: &[postpone::HeldLeg], edge: &Edge, was_live: bool) -> Option<&'static str> {
    if held.is_empty() {
        return None; // not held -> not an add (caller handles the fresh-entry path)
    }
    // 1. same direction as ALL held legs (the gate forbids opening an opposite-direction position on a slug).
    if !held.iter().all(|l| l.entry_dir == edge.dir) {
        return None;
    }
    // 2. strictly bigger than the best held entry by the tau-gain margin.
    let base_net = held.iter().map(|l| l.entry_net).fold(f64::NEG_INFINITY, f64::max);
    if edge.net < base_net + cfg.add_tau_gain {
        return None;
    }
    // 3. count cap (cap=1 => any held slug already at the cap => block).
    if (held.len() as u32) >= cfg.max_positions_per_slug {
        return None;
    }
    // 4. classify via the cross-frame proxy + require the matching flag.
    if was_live {
        cfg.enable_scale_in.then_some("scale-in")
    } else {
        cfg.enable_reentry.then_some("re-entry")
    }
}

/// Apply a SPAWNED submission's outcome on the EVENT LOOP's turn (so all position/exposure mutation is
/// single-threaded). Entry: both-filled => record the position (keep the reservation); else => release the
/// reservation, then on a real one-leg-filled NAKED outcome attempt AUTO-RECOVERY (cancel the resting leg +
/// flatten the filled leg) before the halt backstop (FIX A). Unwind: both-filled => remove the position +
/// decrement exposure; else => the flatten itself left a naked leg -> fail-close (W14). Always clears the
/// slug's in-flight marker.
///
/// RETURNS `Some(slug)` for ANY `SubmitKind::Entry` outcome (lock / abort_clean / abort_ambiguous / recover /
/// naked_halt) so the caller can STAMP a per-slug entry cooldown on EVERY entry resolution — not just the
/// fire site (2026-06-15 churn fix). `None` for Unwind/Recovery (those don't gate a fresh entry).
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_outcome(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    exposure: &mut Exposure,
    pending_entries: &mut std::collections::HashSet<String>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    halt: &std::sync::atomic::AtomicBool,
    out: SubmitOutcome,
) -> Option<String> {
    let both = out.ack.both_filled();
    // exec-log liveness (2026-06-15): a dry-run backend's fire outcomes route to the separate `.dryrun` log.
    let live = backend.label() != "dry-run";
    match out.kind {
        SubmitKind::Entry => {
            pending_entries.remove(&out.slug);
            if both {
                if let (Some(mut pos), Some(pair)) = (out.position, out.pair) {
                    // persist each leg's exchange order id from its fill ack (positional: ack.a <-> legs[0],
                    // ack.b <-> legs[1]) so a later cancel/unwind can reach the right venue endpoint (FIX 3).
                    for (leg, ack) in pos.legs.iter_mut().zip([&out.ack.a, &out.ack.b]) {
                        if let Ok(a) = ack {
                            leg.venue_order_id = a.venue_order_id.clone();
                        }
                    }
                    // APPEND the new leg (exposure stays RESERVED — keep it). cost_per/entry_net/entry_dir are
                    // STORED on the HeldLeg so a later per-leg unwind subtracts EXACTLY this leg (R1) and a
                    // later add gates on max(entry_net)+tau / same-direction.
                    track_position(positions, &pair, pos, out.cost_per, out.entry_net, out.entry_dir);
                    crate::exec_log::fire_outcome(&out.slug, "lock", "both legs filled", live);
                }
            } else {
                // subtract the EXACT reservation made at spawn (the entry did not fully fill) — the unified
                // inverse of reserve_exposure (R1), never the whole-bucket remove.
                if let Some(pos) = &out.position {
                    subtract_exposure(exposure, pos, out.cost_per);
                }
                // pmus-first ABORT (0020): the fast (Kalshi) leg carries the `HedgeNotFilled` sentinel iff we
                // deliberately never opened it (the slow pmus hedge didn't fill first). That's NOT a naked leg
                // (nothing filled) — route it explicitly instead of the recover/fail-close path.
                match classify_entry_miss(&out.ack) {
                    // hedge RESTED (Ok, not filled): no position, but its GTC order would fill later unhedged
                    // -> cancel it (best-effort). No halt — this is the EXPECTED skip on a non-fillable hedge.
                    // (A PERSISTENT phantom hedge could abort-loop place→cancel→re-fire; bounded today by
                    // pending_entries + the entry caps + a pmus rate-limit→AmbiguousAbort halt. A per-slug
                    // abort cooldown is a clean production follow-up — not needed for the gated 1-contract test.)
                    EntryMiss::CleanAbort(rest_idx) => {
                        cancel_resting_hedge(backend, &out.slug, &out.ack, out.position.as_ref(), rest_idx);
                        crate::exec_log::fire_outcome(&out.slug, "abort_clean", "pmus hedge did not fill; cancelled, no position", live);
                    }
                    // hedge ERR'd: its order's fate is UNKNOWN (a transport error may have landed it). Fail
                    // CLOSED — HALT so the owner reconciles before any unhedged pmus order can fill silently.
                    EntryMiss::AmbiguousAbort => {
                        halt.store(true, std::sync::atomic::Ordering::Relaxed);
                        eprintln!(
                            "[live] CRITICAL pmus-first abort on {}: the pmus hedge leg ERR'd (a={:?} b={:?}) — \
                             order fate UNKNOWN (may have landed) -> KILL-SWITCH engaged; reconcile positions before resuming.",
                            out.slug, out.ack.a, out.ack.b
                        );
                        crate::exec_log::fire_outcome(&out.slug, "abort_ambiguous", "pmus hedge err -> halt", live);
                    }
                    // FIX A: a real one-leg-filled outcome is a NAKED directional leg. AUTO-RECOVER (cancel the
                    // resting leg + flatten the filled leg at a marketable book price); the fail-close halt is
                    // the BACKSTOP when recovery can't be priced/fired. Never records a hedge.
                    EntryMiss::NakedOrOther => {
                        // the naked (filled) leg's venue, for the outcome record (which leg went naked).
                        let naked = naked_filled_idx(&out.ack)
                            .and_then(|i| out.position.as_ref().map(|p| format!("{:?}", p.legs[i].venue)))
                            .unwrap_or_default();
                        if !recover_naked_leg(backend, kalshi_books, pmus_books, flattening, outcome_tx, &out.slug, &out.ack, out.position.as_ref()) {
                            naked_leg_failclose(&out.slug, SubmitKind::Entry, &out.ack, halt);
                            crate::exec_log::fire_outcome(&out.slug, "naked_halt", &naked, live);
                        } else {
                            crate::exec_log::fire_outcome(&out.slug, "recover", &naked, live);
                        }
                    }
                }
            }
            // EVERY Entry resolution stamps the per-slug cooldown (the caller inserts on this `Some`) — the
            // fire site already stamped it; re-stamping on resolution extends the window past a churn burst.
            Some(out.slug)
        }
        SubmitKind::Unwind => {
            flattening.remove(&out.slug); // a non-flat outcome lets the poll re-emit to retry
            if both {
                // POP the FRONT leg that was just flattened (spawn_unwind always targets legs[0], and
                // `flattening` serialized this slug so the Vec is unchanged since the spawn — the front is
                // exactly that leg). Subtract its OWN stored cost_per (the exact inverse, R1) — NEVER the
                // whole bucket. Drop the slug key only when its `legs` empties (R5 — else prune/W16 can't
                // fire). A still-non-empty slug keeps the poll re-emitting to flatten the next leg (§1.5).
                let mut drop_slug = false;
                if let Some(sp) = lock(positions).get_mut(&out.slug) {
                    if !sp.legs.is_empty() {
                        let removed = sp.legs.remove(0);
                        subtract_exposure(exposure, &removed.pos, removed.cost_per);
                    }
                    drop_slug = sp.legs.is_empty();
                }
                if drop_slug {
                    lock(positions).remove(&out.slug);
                    println!("[UNWIND] flattened {} (slug fully closed)", out.slug);
                } else {
                    println!("[UNWIND] flattened one leg of {} ({} leg(s) remain; poll re-emits)", out.slug, lock(positions).get(&out.slug).map(|sp| sp.legs.len()).unwrap_or(0));
                }
            } else {
                // a postpone unwind that HALF-filled (one leg sold, the other unfilled) is a NEW naked leg ->
                // halt; a both-failed unwind sold nothing (the pair is still hedged) so the poll re-emits.
                println!("[UNWIND] WARN {} did not fully flatten (one leg unfilled); poll re-emits", out.slug);
                naked_leg_failclose(&out.slug, SubmitKind::Unwind, &out.ack, halt);
            }
            None // an unwind does not gate a fresh entry's cooldown
        }
        SubmitKind::Recovery => {
            // a naked-leg RECOVERY flatten (single SELL of the already-filled leg, fired by FIX A). The
            // outcome's leg `a` is that SELL; leg `b` is unused here.
            flattening.remove(&out.slug);
            if matches!(&out.ack.a, Ok(a) if a.filled) {
                println!("[RECOVER] flattened the naked leg on {} (filled)", out.slug);
            } else {
                // the SELL did NOT fill -> the originally-filled leg is STILL naked. This is the case the
                // simulated-sentinel approach would have slipped past `naked_filled_idx`; halt EXPLICITLY so
                // the unhedged directional leg surfaces for a manual flatten. (Dry-run SELLs fill -> the Ok
                // branch above, so this never trips in dry-run.)
                halt.store(true, std::sync::atomic::Ordering::Relaxed);
                eprintln!(
                    "[live] CRITICAL RECOVERY flatten of the naked leg on {} did NOT fill ({:?}) \
                     -> KILL-SWITCH engaged. The filled leg is STILL a directional position — flatten MANUALLY.",
                    out.slug, out.ack.a
                );
            }
            None // a recovery does not gate a fresh entry's cooldown
        }
    }
}

/// Which leg of a non-both-filled pair is the NAKED one: returns `Some(0)` if leg `a` filled LIVE (not a
/// simulated dry-run ack) while `b` did not fill, `Some(1)` for the mirror, else `None` (no real naked leg
/// — both errored, both simulated, or both filled). "Did not fill" covers an `Err` AND an `Ok`-but-resting
/// (accepted, not filled) leg — a resting leg leaves the other filled leg just as naked as an errored one.
pub(crate) fn naked_filled_idx(ack: &exec::PairAck) -> Option<usize> {
    let live_filled = |r: &Result<exec::Ack, exec::ExecError>| matches!(r, Ok(a) if a.filled && !a.simulated);
    let not_filled = |r: &Result<exec::Ack, exec::ExecError>| !matches!(r, Ok(a) if a.filled);
    if live_filled(&ack.a) && not_filled(&ack.b) {
        Some(0)
    } else if live_filled(&ack.b) && not_filled(&ack.a) {
        Some(1)
    } else {
        None
    }
}

/// pmus-first ABORT classification (decision 0020). A non-both-filled ENTRY whose FAST leg is the
/// `HedgeNotFilled` sentinel means we deliberately never opened it (the slow pmus hedge didn't fill first).
pub(crate) enum EntryMiss {
    /// hedge RESTED (Ok, not filled): cancel its resting GTC order, no halt, NO position. Carries the hedge idx.
    CleanAbort(usize),
    /// hedge ERR'd (its order's fate is unknown — a transport error may have landed it): HALT to reconcile.
    AmbiguousAbort,
    /// NOT a pmus-first abort: a real one-leg-filled naked position, or any other shape -> recover/fail-close.
    NakedOrOther,
}

/// Classify the non-both-filled ENTRY outcome (PURE — unit-testable). The `HedgeNotFilled` sentinel on one
/// leg marks the fast leg we never opened; the OTHER leg is the pmus hedge that actually resolved (rested,
/// errored, or — defensively — filled). No sentinel ⇒ a concurrent-era / real-fill outcome ⇒ `NakedOrOther`.
pub(crate) fn classify_entry_miss(ack: &exec::PairAck) -> EntryMiss {
    let is_sentinel = |r: &Result<exec::Ack, exec::ExecError>| matches!(r, Err(exec::ExecError::HedgeNotFilled));
    let (hedge, hedge_idx) = if is_sentinel(&ack.a) {
        (&ack.b, 1)
    } else if is_sentinel(&ack.b) {
        (&ack.a, 0)
    } else {
        return EntryMiss::NakedOrOther; // no sentinel -> not a pmus-first abort
    };
    match hedge {
        Ok(a) if !a.filled => EntryMiss::CleanAbort(hedge_idx), // rested -> cancel it, no position
        Ok(_) => EntryMiss::NakedOrOther, // hedge FILLED but the other is the sentinel (shouldn't occur) -> recover it
        Err(_) => EntryMiss::AmbiguousAbort, // hedge errored -> order fate unknown -> halt
    }
}

/// Cancel the resting (GTC) pmus hedge after a CLEAN pmus-first abort (0020): the fast leg was never opened
/// so there is NO position, but the hedge order rested and would otherwise fill later UNHEDGED. Best-effort +
/// spawned (a cancel failure is harmless — a resting order that never fills costs nothing). Logs either way.
pub(crate) fn cancel_resting_hedge(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    slug: &str,
    ack: &exec::PairAck,
    position: Option<&Position>,
    rest_idx: usize,
) {
    let rest_ack = if rest_idx == 0 { &ack.a } else { &ack.b };
    if let (Some(pos), Ok(a)) = (position, rest_ack) {
        if !a.venue_order_id.is_empty() && rest_idx < pos.legs.len() {
            let target = exec::CancelTarget {
                venue: pos.legs[rest_idx].venue,
                venue_order_id: a.venue_order_id.clone(),
                market: pos.legs[rest_idx].market.clone(),
            };
            eprintln!("[live] pmus-first ABORT on {slug}: pmus hedge did not fill -> Kalshi leg never opened (NO position); cancelling the resting GTC hedge order.");
            spawn_cancel(backend, slug, target);
            return;
        }
    }
    println!("[live] pmus-first ABORT on {slug}: pmus hedge did not fill -> NO position (no resting order to cancel).");
}

/// FIX A — NAKED-LEG AUTO-RECOVERY. On a one-leg-filled entry outcome: (1) if the UNFILLED leg is an
/// `Ok`-but-resting order with a venue order id, CANCEL it (spawned, best-effort); (2) FLATTEN the FILLED
/// leg with a single marketable SELL (best YES bid for a YES leg, `1 - YES ask` for a NO leg) read from
/// that leg's LIVE book, fired through the single-leg `submit` primitive on a spawned task; the SELL's
/// outcome routes back as a `SubmitKind::Recovery` so a SELL that ITSELF fails to fill re-trips the halt.
/// Does NOT record a hedge; the reservation was already released by the caller. Returns TRUE iff recovery
/// was LAUNCHED; FALSE (caller halts as the backstop) when there is no real naked leg OR the filled leg
/// cannot be priced (one-sided book) / its (venue, market, side) is unavailable. FAIL SAFE: any uncertainty
/// about the flatten price/route returns FALSE -> halt, never an un-flattened silent naked leg.
#[allow(clippy::too_many_arguments)]
pub(crate) fn recover_naked_leg(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: &str,
    ack: &exec::PairAck,
    position: Option<&Position>,
) -> bool {
    let Some(filled_idx) = naked_filled_idx(ack) else {
        return false; // no real (live) naked leg -> nothing to recover; caller's halt is a no-op anyway
    };
    // the held legs (with venue/market/side) we built the entry from — positional: legs[i] <-> ack {a,b}.
    let Some(pos) = position else { return false }; // no leg metadata -> can't price/route -> halt backstop
    let resting_idx = 1 - filled_idx;
    let filled_leg = &pos.legs[filled_idx];
    let resting_ack = if resting_idx == 0 { &ack.a } else { &ack.b };

    // AMBIGUOUS UNFILLED LEG (2026-06-15): under FOK a clean MISS returns Ok(not-filled); an Err means a
    // 4xx-reject or a transport error whose order fate is UNKNOWN — it may have LANDED + FILLED. If we
    // flattened the KNOWN-filled leg now and the "unfilled" leg actually filled too, we'd UN-HEDGE a real
    // LOCK (sell off one side of a both-filled pair). FAIL CLOSED: return false so the caller halts for a
    // manual reconcile, rather than guess. (A clean Ok-but-resting unfilled leg proceeds to recovery below.)
    if resting_ack.is_err() {
        eprintln!(
            "[live] CRITICAL NAKED LEG on {slug}: the unfilled leg ERR'd ({resting_ack:?}) — its order fate is \
             UNKNOWN (may have FILLED) -> NOT flattening (could be a real LOCK); HALTING for manual reconcile."
        );
        return false;
    }

    // W-1: the slug's flatten slot is occupied — by WHAT decides whether this naked leg is covered.
    //   * Recovery: a prior recovery for THIS slug's naked leg is already in flight -> genuinely covered ->
    //     return true (don't double-fire; the in-flight recovery flattens it).
    //   * Unwind: a postpone-unwind holds the slot, but it targets only the FRONT held leg (`legs[0]`), NOT
    //     this freshly-naked add leg. Treating it as "covered" would ABANDON the add leg silently (the W-1
    //     bug). FAIL CLOSED: return false so the caller engages the halt and the unhedged leg surfaces for a
    //     manual flatten. (We can't safely fire a second flatten here either — `flattening` is per-slug and a
    //     second SELL would race the unwind's pop of legs[0]; halt is the correct fail-safe.)
    match flattening.get(slug) {
        Some(FlatKind::Recovery) => return true, // this naked leg's recovery is already underway
        Some(FlatKind::Unwind) => return false,  // unwind covers a DIFFERENT leg -> NOT covered -> caller halts
        None => {}                               // slot free -> fire the recovery below
    }

    // PRICE the filled leg's marketable SELL from its LIVE book. Unpriceable (one-sided book / no book) ->
    // FALSE so the caller halts: we must NOT leave the filled leg silently naked. The flatten is a SELL, so
    // it FLOOR-quantizes to the leg's pmus tick (W2) — `Position` now carries `pm_min_tick` on the pmus leg —
    // before the cent floor, so a coarse-tick pmus market doesn't reject the recovery SELL.
    let book = match filled_leg.venue {
        Venue::Kalshi => lock(kalshi_books).get(&filled_leg.market).map(|b| b.touch()),
        Venue::Pmus => pmus_books.get(&filled_leg.market).map(|b| b.touch()),
    };
    let Some(exit) = book.and_then(|b| flatten_exit_cents(filled_leg, &b)) else {
        eprintln!("[live] CRITICAL NAKED LEG on {slug}: filled {:?} leg can't be priced for a flatten (one-sided book) -> halting", filled_leg.venue);
        return false;
    };

    // (1) CANCEL the resting leg if it is an accepted-but-resting order with a venue order id (an Err leg
    //     created no order to cancel). Best-effort + spawned: a cancel failure still leaves the FLATTEN as
    //     the real risk reducer, and a GTC resting order that never fills is harmless once we're flat.
    if let Ok(a) = resting_ack {
        if !a.filled && !a.venue_order_id.is_empty() {
            let target = exec::CancelTarget {
                venue: pos.legs[resting_idx].venue,
                venue_order_id: a.venue_order_id.clone(),
                market: pos.legs[resting_idx].market.clone(),
            };
            spawn_cancel(backend, slug, target);
        }
    }

    // (2) FLATTEN the filled leg: one marketable SELL of the EXACT (venue, market, side) held, fired on a
    //     spawned task via the single-leg `submit`. Mark the slug `flattening` so a burst can't double-fire.
    let sell = OrderIntent {
        venue: filled_leg.venue,
        market: filled_leg.market.clone(),
        action: Action::Sell,
        side: filled_leg.side,
        price_cents: exit,
        qty: pos.size,
        client_order_id: format!("recover-{slug}-{filled_idx}"),
    };
    eprintln!(
        "[live] CRITICAL NAKED LEG on {slug}: one live leg filled, the other did not -> AUTO-RECOVERING \
         (cancel resting leg + SELL {:?} {:?} {}x @ {}c to flatten). No hedge recorded.",
        sell.venue, sell.side, sell.qty, sell.price_cents
    );
    flattening.insert(slug.to_string(), FlatKind::Recovery);
    spawn_flatten(backend, outcome_tx, slug.to_string(), sell);
    true
}

/// SPAWN a `submit_pair` off a cloned `Arc<backend>` and report the `PairAck` back over `outcome_tx`. The
/// event-loop NEVER calls `submit_pair` inline (it would block the whole `select!` for the two-leg RTT);
/// this detaches the network I/O so the loop keeps draining the unwind / outcome arms.
#[allow(clippy::too_many_arguments)]
pub(crate) fn spawn_submit(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    kind: SubmitKind,
    slug: String,
    legs: [OrderIntent; 2],
    position: Option<Position>,
    pair: Option<LivePair>,
    cost_per: f64,
    entry_net: f64,
    entry_dir: Dir,
) {
    let backend = backend.clone();
    let outcome_tx = outcome_tx.clone();
    tokio::spawn(async move {
        // block_in_place requires the multi-thread runtime (#[tokio::main] full); the submit drives the two
        // signed POSTs concurrently inside it. Running it on a SPAWNED task means only this task parks, not
        // the event loop. catch_unwind GUARANTEES an outcome is reported even if `submit_pair` panics —
        // otherwise the slug stays stuck in pending_entries/flattening forever (never re-tradeable / never
        // re-flattenable). A panic maps to a failed pair, so the outcome arm releases the reservation + slug.
        let ack = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            backend.submit_pair(&legs[0], &legs[1])
        }))
        .unwrap_or_else(|_| {
            eprintln!("[live] CRITICAL submit task panicked for {slug} — reporting a failed pair (slug released)");
            exec::PairAck {
                a: Err(exec::ExecError::Rejected("submit panicked".into())),
                b: Err(exec::ExecError::Rejected("submit panicked".into())),
            }
        });
        let _ = outcome_tx.send(SubmitOutcome { slug, kind, ack, position, pair, cost_per, entry_net, entry_dir });
    });
}

/// SPAWN a single-leg recovery SELL (the flatten) off the cloned `Arc<backend>`, reporting it back as a
/// `SubmitKind::Recovery` outcome whose leg `a` is that SELL (leg `b` is an explicit UNUSED `Err` placeholder
/// — never a "filled" sentinel, so it can't be misread as a fill). The Recovery outcome arm halts iff the
/// SELL did NOT fill (the originally-filled leg is then still naked). catch_unwind guarantees an outcome so
/// the slug never stays stuck in `flattening`.
pub(crate) fn spawn_flatten(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: String,
    sell: OrderIntent,
) {
    let backend = backend.clone();
    let outcome_tx = outcome_tx.clone();
    tokio::spawn(async move {
        let a = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.submit(&sell)))
            .unwrap_or_else(|_| {
                eprintln!("[live] CRITICAL recovery flatten panicked for {slug} — reporting a failed SELL");
                Err(exec::ExecError::Rejected("flatten panicked".into()))
            });
        let ack = exec::PairAck {
            a,
            // leg b is UNUSED for a recovery (only leg a — the SELL — is read). An Err placeholder, never a
            // filled sentinel, so no path can mistake it for a fill.
            b: Err(exec::ExecError::Rejected("recovery has no second leg".into())),
        };
        let _ = outcome_tx.send(SubmitOutcome { slug, kind: SubmitKind::Recovery, ack, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK });
    });
}

/// SPAWN a best-effort cancel of a resting leg off the cloned `Arc<backend>` (the recovery's cancel step).
/// Fire-and-log: the FLATTEN is the real risk reducer, so a cancel failure is logged, not fatal (a GTC
/// resting order that never fills is harmless once the filled leg is flat).
pub(crate) fn spawn_cancel(backend: &std::sync::Arc<dyn ExecutionBackend>, slug: &str, target: exec::CancelTarget) {
    let backend = backend.clone();
    let slug = slug.to_string();
    tokio::spawn(async move {
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.cancel(&target)))
            .unwrap_or_else(|_| Err(exec::ExecError::Rejected("cancel panicked".into())));
        if let Err(e) = r {
            eprintln!("[live] WARN recovery cancel of the resting leg failed for {slug} ({e:?}) — the flatten still reduces the risk; a resting GTC order is harmless once flat");
        }
    });
}

/// W14 FAIL-CLOSE BACKSTOP: a non-both-filled outcome where exactly one leg actually FILLED LIVE while the
/// OTHER did not is a naked directional position. This is the BACKSTOP for when auto-recovery (FIX A) could
/// NOT be launched (no priceable book / no leg metadata) — log CRITICAL and ENGAGE the runtime halt (blocks
/// all new entries), keeping the filled leg visible for a MANUAL flatten. Dry-run acks are simulated +
/// filled -> `both_filled` is always true there, so this never trips in dry-run.
pub(crate) fn naked_leg_failclose(slug: &str, kind: SubmitKind, ack: &exec::PairAck, halt: &std::sync::atomic::AtomicBool) {
    if let Some(filled_idx) = naked_filled_idx(ack) {
        let filled = if filled_idx == 0 { &ack.a } else { &ack.b };
        halt.store(true, std::sync::atomic::Ordering::Relaxed);
        eprintln!(
            "[live] CRITICAL NAKED LEG on {kind:?} {slug}: one live leg filled, the other did not ({filled:?}) \
             -> KILL-SWITCH engaged (no new entries). Filled leg is a directional position — flatten MANUALLY."
        );
    }
}

/// Prepare + SPAWN a postponement unwind for ONE held leg on the slug (design §1.5 — v1 flattens
/// one-at-a-time, FRONT leg first; the poll's per-cycle re-emit picks up the next leg over later cycles). A
/// postponement voids the whole GAME, so EVERY leg on the slug must eventually flatten. This fires the FRONT
/// `HeldLeg`'s two SELLs (priced from the live books); the Unwind outcome arm pops that front leg + subtracts
/// its EXACT `cost_per` (never the whole bucket). Dedupes against an in-flight flatten (`flattening`). A
/// one-sided book (a leg can't be priced) logs a WARN and clears nothing so the next re-emit retries.
/// REDUCE-ONLY: fires even under the kill-switch (flattening a void REDUCES risk); dry-run only LOGS.
#[allow(clippy::too_many_arguments)]
pub(crate) fn spawn_unwind(
    cfg: &Config,
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: &str,
) {
    if flattening.contains_key(slug) {
        return; // a flatten (unwind OR recovery) for this slug is already in flight -> don't double-fire (C5)
    }
    // the FRONT held leg on this slug (one-at-a-time, §1.5). `None`/empty -> already flattened / gone.
    let front = lock(positions).get(slug).and_then(|sp| sp.legs.first().cloned());
    let Some(front) = front else { return };
    if cfg.kill_switch {
        println!("[UNWIND] kill-switch engaged but flattening (reduce-only) {slug}");
    }
    // price each leg's exit from the venue book it sits on (a SELL never blocks on a fresh entry edge).
    let exits = unwind_exit_cents(&front.pos, |leg| match leg.venue {
        Venue::Kalshi => lock(kalshi_books).get(&leg.market).map(|b| b.touch()),
        Venue::Pmus => pmus_books.get(&leg.market).map(|b| b.touch()),
    });
    let Some(exits) = exits else {
        println!("[UNWIND] WARN one-sided book — cannot price both legs of {slug}; holding (poll re-emits)");
        return; // not marked flattening -> the poll's re-emit retries once a book is two-sided
    };
    let orders = unwind::unwind_orders(&front.pos, exits);
    flattening.insert(slug.to_string(), FlatKind::Unwind);
    // carry the front leg's pos + cost_per so the outcome arm subtracts EXACTLY this leg (defensive; the arm
    // pops the front and uses the popped leg's OWN stored cost_per — the exact-release guarantee).
    spawn_submit(backend, outcome_tx, SubmitKind::Unwind, slug.to_string(), orders, Some(front.pos), None, front.cost_per, 0.0, Dir::PK);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pricing::position_from_intents;
    use crate::test_support::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// pmus-first ABORT classification (0020): the `HedgeNotFilled` sentinel on the fast leg + a resting
    /// (Ok-not-filled) pmus hedge = a CLEAN abort (cancel the rest, no halt); an ERR'd hedge = AMBIGUOUS
    /// (halt — order fate unknown); no sentinel = the real naked/recover path (unchanged).
    #[test]
    fn classify_entry_miss_routes_the_pmus_first_abort() {
        let ok = |filled: bool, oid: &str| -> Result<exec::Ack, exec::ExecError> {
            Ok(exec::Ack { client_order_id: "c".into(), venue_order_id: oid.into(), filled, simulated: false })
        };
        let sentinel = || -> Result<exec::Ack, exec::ExecError> { Err(exec::ExecError::HedgeNotFilled) };
        let errd = || -> Result<exec::Ack, exec::ExecError> { Err(exec::ExecError::Rejected("boom".into())) };

        // pmus hedge (leg a) RESTED + fast leg (b) never opened -> CleanAbort(0): cancel the resting hedge
        assert!(matches!(classify_entry_miss(&exec::PairAck { a: ok(false, "PM1"), b: sentinel() }), EntryMiss::CleanAbort(0)));
        // mirror (pmus is leg b) -> CleanAbort(1)
        assert!(matches!(classify_entry_miss(&exec::PairAck { a: sentinel(), b: ok(false, "PM1") }), EntryMiss::CleanAbort(1)));
        // pmus hedge ERR'd (order fate unknown) + sentinel -> AmbiguousAbort -> caller HALTS to reconcile
        assert!(matches!(classify_entry_miss(&exec::PairAck { a: errd(), b: sentinel() }), EntryMiss::AmbiguousAbort));
        // no sentinel (a real one-filled-one-rested outcome) -> NakedOrOther -> the recover/fail-close path
        assert!(matches!(classify_entry_miss(&exec::PairAck { a: ok(true, "K1"), b: ok(false, "PM1") }), EntryMiss::NakedOrOther));
        // defensive: hedge FILLED but the OTHER leg is the sentinel (shouldn't occur) -> NakedOrOther (recover it)
        assert!(matches!(classify_entry_miss(&exec::PairAck { a: ok(true, "PM1"), b: sentinel() }), EntryMiss::NakedOrOther));
    }

    /// `track_position` does NOT enroll a WORLD-CUP pair in the MLB postponement poll (no statsapi WC
    /// source): a WC `Cat::Sports` pair gets EMPTY poll metadata (like weather/econ), while a moneyline MLB
    /// pair still derives league/date/abbrevs. (The held position is still recorded for exposure/dedup.)
    #[test]
    fn track_position_skips_mlb_poll_for_world_cup() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let pair = wc_pair();
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 1, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 55, qty: 1, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        track_position(&positions, &pair, pos, 0.97, 0.03, Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().expect("WC position still recorded (exposure/dedup)");
        // EMPTY poll metadata -> the MLB poll's `league=="mlb"` filter skips it (no wrong unwind / no warning).
        assert_eq!((sp.league.as_str(), sp.date.as_str(), sp.team_a.as_str(), sp.team_b.as_str()), ("", "", "", ""),
            "a WC pair enrolls with EMPTY MLB-poll metadata (it has no statsapi source)");
        assert_eq!(sp.legs.len(), 1, "one leg recorded");
    }

    /// `track_position` then `subtract_exposure` (the unified inverse, R1) round-trips exposure to zero (caps
    /// bind on entry, re-open on flatten), and a SPORTS pair derives league/date/abbrevs for the poll while
    /// non-sports leaves them empty.
    #[test]
    fn reserve_track_and_decrement_exposure_round_trips() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0),
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 4, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 4, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        // RESERVE at spawn (exposure bumps) then APPEND the leg on both-filled (exposure stays reserved).
        reserve_exposure(&mut exp, &pos, 0.97);
        track_position(&positions, &pair, pos, 0.97, 0.03, Dir::PK);
        // exposure reserved by cost_per×size = 0.97×4 = 3.88 across pair/cluster/total; one open position.
        assert!((exp.total - 3.88).abs() < 1e-9);
        assert!((exp.per_pair["aec-mlb-lad-pit-2026-06-16"] - 3.88).abs() < 1e-9);
        assert!((exp.per_cluster["mlb-2026-06-16"] - 3.88).abs() < 1e-9);
        assert_eq!(exp.open_positions, 1);
        // the held slug carries the poll's match fields (league/date/abbrevs from the slug + tickers).
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!((sp.league.as_str(), sp.date.as_str(), sp.team_a.as_str(), sp.team_b.as_str()), ("mlb", "2026-06-16", "lad", "pit"));
        assert_eq!(sp.legs.len(), 1);
        // flatten the (only) leg with its STORED cost_per (the exact inverse, R1) -> exposure back to zero.
        // Post-R1, per_pair is value-subtracted to ~0 (NOT key-removed; the bucket key is dropped lazily with
        // the slug). The asserted SEMANTICS (round-trips to zero) are unchanged; the shape of the last check
        // is the per-pair VALUE, not key-absence.
        let leg0 = sp.legs[0].clone();
        subtract_exposure(&mut exp, &leg0.pos, leg0.cost_per);
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0);
        assert!(exp.per_pair.get("aec-mlb-lad-pit-2026-06-16").copied().unwrap_or(0.0).abs() < 1e-9);
    }

    /// C6: re-tracking a slug that already has a HeldPosition PRESERVES the poll's accumulated `prev`
    /// (status history) instead of clobbering it to `None` — the officialDate-slide detection needs `prev`.
    #[test]
    fn track_position_preserves_prev_on_retrack() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0),
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 4, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 4, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        track_position(&positions, &pair, pos.clone(), 0.97, 0.03, Dir::PK);
        // the poll has since accumulated a status snapshot on this slug.
        let snapshot = postpone::GameStatus { detailed_state: "Scheduled".into(), official_date: Some("2026-06-16".into()), ..Default::default() };
        positions.lock().unwrap().get_mut(&pair.slug).unwrap().prev = Some(snapshot.clone());
        // an ADD (scale-in/re-entry) APPENDS a second leg on the SAME slug — `prev` is slug-level so it must
        // survive (it was clobbered to None pre-C6; now the preservation is STRUCTURAL — append never touches
        // prev). The append also stacks a 2nd leg (the multi-position shape).
        track_position(&positions, &pair, pos, 0.96, 0.05, Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!(sp.prev, Some(snapshot), "prev survives an append (slug-level, never re-derived per leg)");
        assert_eq!(sp.legs.len(), 2, "the add stacked a second leg");
    }

    fn sim_ack(coid: &str) -> Result<exec::Ack, exec::ExecError> {
        Ok(exec::Ack { client_order_id: coid.into(), venue_order_id: "SIMULATED".into(), filled: true, simulated: true })
    }
    fn live_ack(coid: &str) -> Result<exec::Ack, exec::ExecError> {
        Ok(exec::Ack { client_order_id: coid.into(), venue_order_id: "v1".into(), filled: true, simulated: false })
    }
    fn wx_entry_pair() -> (LivePair, Position, f64) {
        let pair = LivePair {
            slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            kalshi: "KXHIGHNY-26JUN11-T95".into(),
            kalshi_b: None, cat: Cat::Weather, cluster: "nychigh-2026-06-11".into(), settle_clean: true, soccer: false, days_to_event: None,
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "xarb-…-B".into() },
        ];
        (pair.clone(), position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs), 0.97)
    }

    /// A test harness for `apply_outcome` that supplies the FIX-A recovery args (backend + books + outcome
    /// channel). `kalshi_books`/`pmus_books` are empty by default (no priceable flatten -> recovery declines
    /// and the halt backstop runs) unless a test pre-populates them. Returns the `outcome_rx` so a test can
    /// assert whether a recovery SELL was actually spawned.
    #[allow(clippy::too_many_arguments)]
    fn run_apply(
        backend: &std::sync::Arc<dyn ExecutionBackend>,
        positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
        kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
        pmus_books: &std::collections::HashMap<String, book::PmusBook>,
        exp: &mut Exposure,
        pending: &mut std::collections::HashSet<String>,
        flat: &mut std::collections::HashMap<String, FlatKind>,
        halt: &AtomicBool,
        out: SubmitOutcome,
    ) -> tokio::sync::mpsc::UnboundedReceiver<SubmitOutcome> {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        apply_outcome(backend, positions, kalshi_books, pmus_books, exp, pending, flat, &tx, halt, out);
        rx
    }

    /// Like `run_apply` but RETURNS `apply_outcome`'s `Option<String>` — the per-slug cooldown contract the
    /// whole churn-cooldown depends on (Some(slug) for an Entry outcome, None for Unwind/Recovery).
    #[allow(clippy::too_many_arguments)]
    fn run_apply_ret(
        backend: &std::sync::Arc<dyn ExecutionBackend>,
        positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
        kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
        pmus_books: &std::collections::HashMap<String, book::PmusBook>,
        exp: &mut Exposure,
        pending: &mut std::collections::HashSet<String>,
        flat: &mut std::collections::HashMap<String, FlatKind>,
        halt: &AtomicBool,
        out: SubmitOutcome,
    ) -> Option<String> {
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        apply_outcome(backend, positions, kalshi_books, pmus_books, exp, pending, flat, &tx, halt, out)
    }

    /// COOLDOWN CONTRACT (2026-06-15): `apply_outcome` returns `Some(slug)` for EVERY Entry outcome (so the
    /// loop stamps the per-slug cooldown that stops churn) and `None` for Unwind/Recovery (those must not cool
    /// an entry slug). The whole churn-cooldown rests on this return value; pin it.
    #[test]
    fn apply_outcome_returns_cooldown_slug_for_entry_only() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // ENTRY (both-filled lock) -> Some(slug)
        let entry = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: sim_ack("a"), b: sim_ack("b") }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        assert_eq!(run_apply_ret(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, entry).as_deref(), Some(slug.as_str()), "an Entry outcome cools its slug");
        // UNWIND -> None (the cooldown gates ENTRIES, never unwinds)
        let unwind = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Unwind, ack: exec::PairAck { a: sim_ack("a"), b: sim_ack("b") }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        assert_eq!(run_apply_ret(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind), None, "Unwind does not cool the entry slug");
        // RECOVERY (its SELL filled) -> None
        let recovery = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Recovery, ack: exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::Rejected("unused".into())) }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        assert_eq!(run_apply_ret(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, recovery), None, "Recovery does not cool the entry slug");
    }

    fn dry_backend() -> std::sync::Arc<dyn ExecutionBackend> {
        std::sync::Arc::new(exec::DryRunBackend)
    }
    fn empty_kbooks() -> std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>> {
        std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new()))
    }

    /// CORE: a BOTH-FILLED entry outcome RECORDS the position and KEEPS the spawn reservation (exposure
    /// unchanged from the reserve), and clears the slug's `pending_entries` marker.
    #[test]
    fn outcome_entry_both_filled_records_and_keeps_reservation() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        // SPAWN-time bookkeeping: reserve + mark pending.
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        let reserved_total = exp.total;
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: sim_ack("a"), b: sim_ack("b") }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!((exp.total - reserved_total).abs() < 1e-9, "both-filled keeps the reservation");
        assert!(positions.lock().unwrap().contains_key(&slug), "position recorded");
        assert!(!pending.contains(&slug), "pending marker cleared");
        assert!(!halt.load(Ordering::Relaxed), "a clean simulated fill never halts");
    }

    /// CORE: a NON-both-filled entry outcome RELEASES the exact reservation (exposure back to zero) and
    /// clears `pending_entries`. A SIMULATED partial (dry-run can't produce one, but a transport error can
    /// in live with keys absent) does NOT trip the naked-leg halt because neither leg actually filled live.
    #[test]
    fn outcome_entry_failed_releases_reservation() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // both legs errored (e.g. KeysUnavailable) -> not both_filled, no live fill -> release, no halt.
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: Err(exec::ExecError::KeysUnavailable), b: Err(exec::ExecError::KeysUnavailable) }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "reservation released exactly");
        assert!(!positions.lock().unwrap().contains_key(&slug), "no position recorded on a failed entry");
        assert!(!pending.contains(&slug));
        assert!(!halt.load(Ordering::Relaxed), "no LIVE leg filled -> no naked-leg halt");
    }

    /// W14 FAIL-CLOSE BACKSTOP: an entry where ONE leg filled LIVE and the other errored is naked. When the
    /// filled leg CANNOT be priced for a flatten (no live book here), auto-recovery (FIX A) declines and the
    /// halt backstop engages (blocks all new entries) + the reservation is still released.
    #[test]
    fn outcome_naked_live_leg_engages_halt_when_recovery_unpriceable() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // leg A filled LIVE, leg B rate-limited -> NAKED. No book is available -> recovery can't price the
        // flatten -> the halt backstop runs (this is the FAIL-SAFE: never leave the leg silently naked).
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("a"), b: Err(exec::ExecError::RateLimited) }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(halt.load(Ordering::Relaxed), "an unpriceable naked LIVE leg must engage the kill-switch backstop");
        assert!(exp.total.abs() < 1e-9, "the entry reservation is still released");
        assert!(!pending.contains(&slug));
        // a SIMULATED-only partial does NOT halt (dry-run safety): one simulated ok + one error.
        let halt2 = AtomicBool::new(false);
        naked_leg_failclose("s", SubmitKind::Entry, &exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }, &halt2);
        assert!(!halt2.load(Ordering::Relaxed), "a simulated partial is not a real naked leg");
        assert_eq!(naked_filled_idx(&exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }), None, "a simulated leg is not a live naked leg");
    }

    /// CORE: a BOTH-FILLED unwind outcome REMOVES the held position + decrements exposure + clears the
    /// `flattening` marker (so the slug is fully closed).
    #[test]
    fn outcome_unwind_both_filled_removes_position() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        // an open, recorded position with its reservation, now mid-flatten.
        reserve_exposure(&mut exp, &pos, cp);
        track_position(&positions, &pair, pos, cp, 0.03, Dir::PK);
        flat.insert(slug.clone(), FlatKind::Unwind);
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Unwind, ack: exec::PairAck { a: sim_ack("u0"), b: sim_ack("u1") }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(!positions.lock().unwrap().contains_key(&slug), "position removed on flatten");
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "exposure decremented on flatten");
        assert!(!flat.contains_key(&slug), "flattening marker cleared");
    }

    /// FIX 3: a both-filled entry PERSISTS each leg's exchange order id (from its fill ack, positionally:
    /// ack.a -> legs[0], ack.b -> legs[1]) onto the tracked position, so a later cancel/unwind can build a
    /// `CancelTarget` and reach the right venue endpoint. Pre-fix the id was parsed then dropped.
    #[test]
    fn outcome_entry_persists_venue_order_ids_on_legs() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // both legs filled live with DISTINCT venue order ids.
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-ORD-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-ORD-2".into(), filled: true, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        let sp = positions.lock().unwrap().get(&slug).cloned().expect("position recorded");
        // the single appended leg's two PositionLegs: leg 0 = pmus (PM-ORD-1), leg 1 = Kalshi (K-ORD-2) —
        // ids persisted onto the held legs (FIX 3).
        let held = &sp.legs[0].pos;
        assert_eq!(held.legs[0].venue_order_id, "PM-ORD-1");
        assert_eq!(held.legs[1].venue_order_id, "K-ORD-2");
        assert!(!halt.load(Ordering::Relaxed), "a clean both-filled live entry does not halt");
    }

    /// FIX 1 end-to-end: an entry where one leg FILLED live and the other came back `Ok` but RESTING
    /// (accepted, not filled) is NOT a hedge — `apply_outcome` must NOT record a position, must release the
    /// reservation. The filled leg is naked; with no priceable book here, the FIX-A recovery declines and the
    /// halt backstop engages. (This is the exact case the pre-fix `is_ok()`-only `both_filled` mis-recorded.)
    #[test]
    fn outcome_entry_one_resting_leg_is_naked_not_a_hedge() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // leg A filled live; leg B ACCEPTED but resting (Ok, filled:false) -> not both-filled -> naked.
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "K-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "PM-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(!positions.lock().unwrap().contains_key(&slug), "a one-resting-leg entry is NOT recorded as a hedge");
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "reservation released");
        assert!(halt.load(Ordering::Relaxed), "the filled leg is naked + unpriceable -> halt backstop engaged");
        assert!(!pending.contains(&slug));
    }

    /// FIX A — NAKED-LEG AUTO-RECOVERY (the task's required test): a one-leg-filled entry where the FILLED
    /// leg CAN be priced from a live book triggers recovery instead of a bare halt — a CANCEL of the resting
    /// leg + a SELL of the filled leg are spawned, NO position is recorded, NO held hedge, and the halt is
    /// NOT engaged (the position is being flattened, not left naked). Leg A = YES@pmus filled live; leg B =
    /// NO@Kalshi resting. The pmus book quotes a YES bid, so the filled YES@pmus leg flattens at that bid.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn outcome_naked_leg_recovers_with_cancel_and_sell() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair(); // leg0 = YES@pmus(slug); leg1 = NO@Kalshi(ticker)
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // the pmus book for the FILLED leg's market quotes a YES bid (0.06) so the flatten SELL can be priced.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.06, 500.0)], &[(0.08, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        // leg A (pmus) filled LIVE; leg B (Kalshi) resting with a venue order id (so it gets cancelled).
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // RECOVERY launched: no held hedge recorded, reservation released, NOT a bare halt, slug marked flattening.
        assert!(!positions.lock().unwrap().contains_key(&slug), "recovery records NO hedge");
        assert!(exp.total.abs() < 1e-9, "the entry reservation is released");
        assert!(!halt.load(Ordering::Relaxed), "recovery flattens -> does NOT engage the halt backstop");
        assert!(flat.contains_key(&slug), "the slug is marked flattening (dedup against a double-fire)");
        assert!(!pending.contains(&slug), "the entry in-flight marker is cleared");
        // the spawned flatten reports a RECOVERY outcome (the dry-run SELL fills): drain it to confirm a SELL
        // was actually fired (dry-run `submit` returns a simulated filled ack -> the recovery completes).
        let recovered = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.expect("recovery outcome timed out").expect("an outcome was sent");
        assert_eq!(recovered.kind, SubmitKind::Recovery, "the flatten routes back as a Recovery outcome");
        assert!(matches!(&recovered.ack.a, Ok(a) if a.filled), "leg a is the SELL and the dry-run flatten fills");
    }

    /// FIX W2 end-to-end: a one-leg-filled entry whose FILLED leg is a coarse-tick pmus market still RECOVERS
    /// (the floored SELL is a valid tick, so the flatten is priceable and recovery launches) — it does NOT
    /// fall through to the halt backstop the way an un-quantized whole-cent SELL would on a coarse market.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn coarse_tick_pmus_leg_still_recovers() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        // a weather PK pair whose pmus market has a coarse 0.05 tick -> the held pmus leg carries it (W2).
        let pair = LivePair {
            slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            kalshi: "KXHIGHNY-26JUN11-T95".into(),
            kalshi_b: None, cat: Cat::Weather, cluster: "nychigh-2026-06-11".into(), settle_clean: true, soccer: false, days_to_event: None,
            pm_min_tick: Some(0.05), pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 10, qty: 2, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 88, qty: 2, client_order_id: "xarb-…-B".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        // the tick is recorded ONLY on the pmus leg (W2); the Kalshi leg carries None.
        assert_eq!(pos.legs[0].pm_min_tick, Some(0.05), "pmus leg carries the tick");
        assert_eq!(pos.legs[1].pm_min_tick, None, "Kalshi leg carries no pmus tick");
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, 0.97);
        pending.insert(slug.clone());
        // the pmus book for the FILLED leg quotes a YES bid of 0.93 (not a 0.05 multiple) -> the flatten SELL
        // floors to 0.90 = 90c (a valid tick) and recovery launches; un-quantized it would have been 93c.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.93, 500.0)], &[(0.95, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: 0.97, entry_net: 0.03, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // RECOVERY launched (not the halt backstop): the coarse-tick SELL was priceable.
        assert!(!halt.load(Ordering::Relaxed), "a coarse-tick pmus leg recovers -> does NOT halt");
        assert!(flat.contains_key(&slug), "recovery launched (slug marked flattening)");
        let recovered = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.expect("recovery outcome timed out").expect("an outcome was sent");
        assert_eq!(recovered.kind, SubmitKind::Recovery, "the floored flatten fires and routes back as Recovery");
    }

    /// FIX A — the FAILED-recovery safety net (the self-review CRITICAL): when the recovery flatten SELL
    /// itself does NOT fill, the originally-filled leg is STILL naked, so the `Recovery` outcome MUST engage
    /// the halt. (The earlier simulated-sentinel design slipped this past `naked_filled_idx` with no halt.)
    #[test]
    fn recovery_flatten_that_does_not_fill_engages_halt() {
        use std::sync::{Arc, Mutex};
        let positions: Arc<Mutex<std::collections::HashMap<String, postpone::SlugPositions>>> = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        flat.insert("s".to_string(), FlatKind::Recovery); // a recovery flatten is in flight for this slug
        // the recovery SELL came back rate-limited (did NOT fill); leg b is the unused Err placeholder.
        let ack = exec::PairAck { a: Err(exec::ExecError::RateLimited), b: Err(exec::ExecError::Rejected("recovery has no second leg".into())) };
        let out = SubmitOutcome { slug: "s".into(), kind: SubmitKind::Recovery, ack, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(halt.load(Ordering::Relaxed), "a recovery SELL that did not fill leaves a naked leg -> halt");
        assert!(!flat.contains_key("s"), "the flattening marker is cleared either way");
        // and a recovery SELL that DID fill clears cleanly without halting.
        let halt2 = AtomicBool::new(false);
        let mut flat2: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat2.insert("s".to_string(), FlatKind::Recovery);
        let ok = exec::PairAck { a: Ok(exec::Ack { client_order_id: "r".into(), venue_order_id: "v".into(), filled: true, simulated: false }), b: Err(exec::ExecError::Rejected("recovery has no second leg".into())) };
        let out2 = SubmitOutcome { slug: "s".into(), kind: SubmitKind::Recovery, ack: ok, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat2, &halt2, out2);
        assert!(!halt2.load(Ordering::Relaxed), "a filled recovery SELL clears without halting");
    }

    /// `naked_filled_idx` pinpoints the live-filled leg: leg-a live-filled + b unfilled -> Some(0); the
    /// mirror -> Some(1); both filled / both errored / a simulated fill -> None (no real naked leg).
    #[test]
    fn naked_filled_idx_identifies_the_live_leg() {
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: Err(exec::ExecError::RateLimited) }), Some(0));
        assert_eq!(naked_filled_idx(&exec::PairAck { a: Err(exec::ExecError::RateLimited), b: live_ack("b") }), Some(1));
        // a resting (Ok, filled:false) other leg still leaves the filled leg naked.
        let resting = Ok(exec::Ack { client_order_id: "r".into(), venue_order_id: "v".into(), filled: false, simulated: false });
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: resting }), Some(0));
        // both filled -> no naked leg; both errored -> none; a simulated fill is not a LIVE naked leg.
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: live_ack("b") }), None);
        assert_eq!(naked_filled_idx(&exec::PairAck { a: Err(exec::ExecError::RateLimited), b: Err(exec::ExecError::RateLimited) }), None);
        assert_eq!(naked_filled_idx(&exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }), None);
    }

    // ====================================================================================================
    // SCALE-IN + RE-ENTRY (multi-position-per-slug) — design tasks/scale-in-reentry-design.md §6 (10 tests)
    // ====================================================================================================

    /// Build a `SlugPositions` with `n` held legs on one slug, each leg reserving `cost_per*size`, recording
    /// `entry_net`/`entry_dir` — and bump `exp` by the SAME reservation each leg (mirrors the spawn-time
    /// reserve so the map and the exposure buckets agree, the real invariant). Returns the slug.
    fn seed_legs(positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>, exp: &mut Exposure, pair: &LivePair, costs_nets: &[(f64, f64)], dir: Dir) -> String {
        let mut slug = String::new();
        for (i, &(cost_per, entry_net)) in costs_nets.iter().enumerate() {
            let legs = [
                OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: format!("a{i}") },
                OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: format!("b{i}") },
            ];
            let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
            reserve_exposure(exp, &pos, cost_per);
            track_position(positions, pair, pos, cost_per, entry_net, dir);
            slug = pair.slug.clone();
        }
        slug
    }

    /// A both-filled Unwind outcome for `slug` (the bookkeeping the poll's re-emit drives one-at-a-time).
    fn unwind_both_filled(slug: &str) -> SubmitOutcome {
        SubmitOutcome { slug: slug.into(), kind: SubmitKind::Unwind, ack: exec::PairAck { a: sim_ack("u0"), b: sim_ack("u1") }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK }
    }

    /// TEST 1 (THE DESYNC REGRESSION — design §6.1 / R1): two positions A+B stacked on ONE slug. Decrement A
    /// (the FRONT leg, via the real Unwind outcome path) -> the per-pair/cluster/total buckets equal exactly
    /// B's contribution (NOT zero — the old whole-bucket `remove` wiped BOTH), open=1, the slug is KEPT.
    /// Decrement B -> buckets ~0, open=0, the slug KEY is gone (R5). This is the exact desync the
    /// one-position guard used to prevent; it MUST be airtight.
    #[test]
    fn test1_multi_position_exact_reserve_and_release_no_desync() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // A: cost_per 0.90, B: cost_per 0.95 (DISTINCT so a desync — subtracting the wrong amount — is
        // detectable). size = 3 each (from seed_legs). A is the FRONT leg (appended first).
        let (ca, cb, size) = (0.90_f64, 0.95_f64, 3.0_f64);
        let slug = seed_legs(&positions, &mut exp, &pair, &[(ca, 0.03), (cb, 0.05)], Dir::PK);
        // reserved = A + B on every bucket; two open positions.
        let want_a = ca * size;
        let want_b = cb * size;
        assert!((exp.per_pair[&slug] - (want_a + want_b)).abs() < 1e-9, "per_pair = A + B reserved");
        assert!((exp.per_cluster[&pair.cluster] - (want_a + want_b)).abs() < 1e-9);
        assert!((exp.total - (want_a + want_b)).abs() < 1e-9);
        assert_eq!(exp.open_positions, 2);
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 2);

        // DECREMENT A (front leg) via the real both-filled Unwind outcome arm.
        flat.insert(slug.clone(), FlatKind::Unwind);
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
        // buckets now equal EXACTLY B's contribution (the desync would have wiped the whole bucket to 0).
        assert!((exp.per_pair[&slug] - want_b).abs() < 1e-9, "after A: per_pair == B exactly (NOT 0 — the old desync)");
        assert!((exp.per_cluster[&pair.cluster] - want_b).abs() < 1e-9, "after A: per_cluster == B");
        assert!((exp.total - want_b).abs() < 1e-9, "after A: total == B");
        assert_eq!(exp.open_positions, 1, "one position still open");
        assert!(positions.lock().unwrap().contains_key(&slug), "slug KEPT while B remains (R5)");
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "B is the remaining leg");

        // DECREMENT B (now the front leg) -> everything to ~0, slug key gone.
        flat.insert(slug.clone(), FlatKind::Unwind);
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
        assert!(exp.per_pair.get(&slug).copied().unwrap_or(0.0).abs() < 1e-9, "after B: per_pair ~0");
        assert!(exp.per_cluster.get(&pair.cluster).copied().unwrap_or(0.0).abs() < 1e-9, "after B: per_cluster ~0");
        assert!(exp.total.abs() < 1e-9, "after B: total ~0");
        assert_eq!(exp.open_positions, 0, "no positions open");
        assert!(!positions.lock().unwrap().contains_key(&slug), "slug KEY gone once legs empty (R5)");
    }

    /// TEST 2 (design §6.2): `track_position` APPENDS a leg + PRESERVES the shared `prev`. (The C6
    /// preservation is now structural; this pins it for the append path explicitly.)
    #[test]
    fn test2_track_position_appends_and_preserves_prev() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let pair = wx_pair();
        seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        // the poll accumulates a prev snapshot on the slug.
        let snap = postpone::GameStatus { detailed_state: "Scheduled".into(), official_date: Some("2026-06-11".into()), ..Default::default() };
        positions.lock().unwrap().get_mut(&pair.slug).unwrap().prev = Some(snap.clone());
        // a second add APPENDS (now 2 legs) and leaves prev untouched.
        seed_legs(&positions, &mut exp, &pair, &[(0.95, 0.05)], Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!(sp.legs.len(), 2, "the second add appended a leg");
        assert_eq!(sp.prev, Some(snap), "prev (slug-level) survived the append");
    }

    /// A config with the add knobs set (else everything stays at the safe defaults).
    fn add_cfg(scale_in: bool, reentry: bool, cap: u32) -> Config {
        let mut c = crate::config::Config::test_default();
        c.enable_scale_in = scale_in;
        c.enable_reentry = reentry;
        c.max_positions_per_slug = cap;
        c.add_tau_gain = 0.01;
        c
    }
    fn held_leg(entry_net: f64, dir: Dir) -> postpone::HeldLeg {
        let leg = OrderIntent { venue: Venue::Pmus, market: "s".into(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "x".into() };
        let pos = position_from_intents("s", Cat::Weather, "c", None, &[leg.clone(), OrderIntent { venue: Venue::Kalshi, market: "K".into(), side: Side::No, ..leg }]);
        postpone::HeldLeg { pos, cost_per: 0.9, entry_net, entry_dir: dir }
    }

    /// TEST 3 (design §6.3): SCALE-IN vs RE-ENTRY classification, each flag independently admits/blocks.
    /// `was_live` (the §3 proxy) selects which flag gates the add.
    #[test]
    fn test3_scalein_vs_reentry_each_flag_gates_independently() {
        let held = [held_leg(0.03, Dir::PK)];
        let bigger = Edge { net: 0.06, dir: Dir::PK }; // > 0.03 + 0.01 tau -> qualifies on the net gate
        // edge LIVE (was_live=true) -> SCALE-IN: needs enable_scale_in.
        assert_eq!(qualifying_add(&add_cfg(true, false, 2), &held, &bigger, true), Some("scale-in"));
        assert_eq!(qualifying_add(&add_cfg(false, true, 2), &held, &bigger, true), None, "scale-in blocked when only re-entry is armed");
        // edge CLOSED (was_live=false) -> RE-ENTRY: needs enable_reentry.
        assert_eq!(qualifying_add(&add_cfg(false, true, 2), &held, &bigger, false), Some("re-entry"));
        assert_eq!(qualifying_add(&add_cfg(true, false, 2), &held, &bigger, false), None, "re-entry blocked when only scale-in is armed");
        // both armed -> the proxy picks the kind.
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &held, &bigger, true), Some("scale-in"));
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &held, &bigger, false), Some("re-entry"));
    }

    /// TEST 4 (design §6.4 / R6): the add_tau_gain gate + the same-direction requirement. An opposite-dir
    /// bigger arb is NOT an add (the gate forbids opening an opposite-direction position on a held slug).
    #[test]
    fn test4_tau_gain_and_same_direction_required() {
        let c = add_cfg(true, true, 2); // both armed + cap 2 so only the net/dir gates can block
        let held = [held_leg(0.03, Dir::PK)];
        // below tau: net 0.035 < 0.03 + 0.01 -> BLOCK (not enough gain over the base).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.035, dir: Dir::PK }, true), None, "add must beat base by >= tau");
        // exactly at tau boundary: 0.04 == 0.03 + 0.01 -> ADMIT (>= is inclusive).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.04, dir: Dir::PK }, true), Some("scale-in"), "net == base + tau passes (>=)");
        // OPPOSITE direction, even much bigger -> BLOCK (R6: never open an opposite-direction position).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.20, dir: Dir::KP }, true), None, "opposite-direction bigger arb is NOT an add");
        // gate must compare against the MAX held entry_net: two held legs (0.03, 0.06) -> base 0.06.
        let two = [held_leg(0.03, Dir::PK), held_leg(0.06, Dir::PK)];
        assert_eq!(qualifying_add(&add_cfg(true, true, 3), &two, &Edge { net: 0.065, dir: Dir::PK }, true), None, "must beat MAX(entry_net)=0.06 by tau, 0.065 < 0.07");
        assert_eq!(qualifying_add(&add_cfg(true, true, 3), &two, &Edge { net: 0.07, dir: Dir::PK }, true), Some("scale-in"));
    }

    /// TEST 5 (design §6.5): max_positions_per_slug binds. cap=1 => the FIRST add is blocked (default
    /// behavior); cap=2 => a 3rd add (when 2 are held) is blocked.
    #[test]
    fn test5_max_positions_per_slug_cap_binds() {
        let bigger = Edge { net: 0.10, dir: Dir::PK };
        let one = [held_leg(0.03, Dir::PK)];
        // cap=1, one held -> at the cap -> BLOCK (this is exactly the one-position-per-slug default).
        assert_eq!(qualifying_add(&add_cfg(true, true, 1), &one, &bigger, true), None, "cap=1 blocks the first add");
        // cap=2, one held -> room -> ADMIT.
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &one, &bigger, true), Some("scale-in"));
        // cap=2, TWO held -> at the cap -> BLOCK the 3rd.
        let two = [held_leg(0.03, Dir::PK), held_leg(0.05, Dir::PK)];
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &two, &bigger, true), None, "cap=2 blocks the 3rd add");
    }

    /// TEST 6 (design §6.6): notional caps bind ACROSS positions — pos#1 using the full per-pair room makes
    /// an otherwise-qualifying add `Reject::PairCap` in `evaluate` (the held leg's contribution is already in
    /// the per_pair bucket, so `pair_room` is what's LEFT). The cap is the real concentration bound the guard
    /// never enforced. (Cluster/total bind the same way — same `evaluate` arithmetic.)
    #[test]
    fn test6_notional_caps_bind_across_positions() {
        use crate::risk::{evaluate, Exposure, Reject};
        let mut c = add_cfg(true, true, 5);
        c.max_notional_per_pair = 1.0; // tiny per-pair cap
        c.max_contracts_per_pair = 100;
        let q = q_pk(); // a clean weather arb, dir PK, settle_clean
        let edge = Edge { net: 0.06, dir: Dir::PK };
        // pos#1 already consumed the whole per-pair cap (1.0) -> the qualifying add has zero pair_room.
        let mut exp = Exposure::new();
        exp.per_pair.insert(q.market.clone(), 1.0); // full per-pair notional in use
        exp.open_positions = 1;
        assert_eq!(evaluate(&c, &q, &edge, &exp, 1000), Err(Reject::PairCap), "per-pair cap binds the SUM across stacked positions");
        // and the cluster cap binds identically (fresh exp, cluster full).
        let mut exp2 = Exposure::new();
        exp2.per_cluster.insert(q.cluster.clone(), c.max_notional_per_cluster);
        exp2.open_positions = 1;
        assert_eq!(evaluate(&c, &q, &edge, &exp2, 1000), Err(Reject::ClusterCap), "per-cluster cap binds across positions");
    }

    /// TEST 7 (design §6.7 / §1.4): a half-filled ADD is recovered from its OWN outcome, and the
    /// already-held base position's exposure is left INTACT. The add's spawn-reservation is released exactly
    /// (subtract this attempt's `cost_per*size`), the add's own naked leg is recovered/halted — the base
    /// leg's contribution and the base leg itself are untouched.
    #[test]
    fn test7_recovery_picks_own_leg_base_position_intact() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // a base position is HELD (reserved 0.90*3) and recorded as one leg.
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        let base_total = exp.total;
        let base_pair = exp.per_pair[&slug];
        assert_eq!(exp.open_positions, 1);
        // the ADD is spawn-reserved (0.95*3) on top, then comes back HALF-FILLED (leg A live, leg B errored)
        // -> NOT both_filled -> release the ADD's exact reservation; the add's naked leg can't be priced (no
        // book) -> halt backstop. Crucially this touches ONLY the add's reservation, not the base's.
        let add_legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "a2".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "b2".into() },
        ];
        let add_pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs);
        reserve_exposure(&mut exp, &add_pos, 0.95);
        pending.insert(slug.clone());
        assert_eq!(exp.open_positions, 2, "base + the in-flight add reserved");
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("a2"), b: Err(exec::ExecError::RateLimited) }, position: Some(add_pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        // the ADD's reservation is released EXACTLY -> exposure is back to the BASE-only amount (intact).
        assert!((exp.total - base_total).abs() < 1e-9, "base exposure intact: only the add's reservation released");
        assert!((exp.per_pair[&slug] - base_pair).abs() < 1e-9, "base per-pair contribution untouched");
        assert_eq!(exp.open_positions, 1, "back to one open position (the base)");
        // the base leg itself is STILL HELD (the failed add never recorded a leg; recovery touched its own leg).
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "the held BASE leg is intact");
        assert!(halt.load(Ordering::Relaxed), "the add's unpriceable naked leg engaged the halt backstop");
    }

    /// TEST 8 (design §6.8): a postpone unwinds ALL positions over re-emit cycles, each removed + decremented
    /// individually, and the slug key drops only after the LAST leg flattens. (Models the poll's per-cycle
    /// re-emit: spawn the front, the outcome pops it, repeat.) Three stacked legs here.
    #[test]
    fn test8_postpone_unwinds_all_positions_over_re_emit_cycles() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03), (0.92, 0.05), (0.95, 0.08)], Dir::PK);
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 3);
        assert_eq!(exp.open_positions, 3);
        // three re-emit cycles, each flattens ONE front leg.
        for remaining in (0..3).rev() {
            flat.insert(slug.clone(), FlatKind::Unwind);
            run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
            assert_eq!(exp.open_positions, remaining, "one position closed per cycle");
            let still_held = positions.lock().unwrap().contains_key(&slug);
            if remaining > 0 {
                assert!(still_held, "slug kept while {remaining} leg(s) remain");
                assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len() as u32, remaining);
            } else {
                assert!(!still_held, "slug key gone after the last leg flattens (R5)");
            }
        }
        assert!(exp.total.abs() < 1e-9, "all exposure decremented exactly to zero");
        assert!(!halt.load(Ordering::Relaxed), "a clean serial flatten never halts");
    }

    /// TEST 9 (design §6.9): dry-run is honored for an armed-flags add — `apply_outcome` of a both-filled
    /// dry-run entry while a position is already held APPENDS the leg (no network: the DryRunBackend only
    /// simulates), exposure stays reserved, no halt. The arming changes WHICH adds the loop admits; it never
    /// changes the execution backend.
    #[test]
    fn test9_dry_run_honored_for_armed_add() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // one held leg already.
        seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        let reserved_after_one = exp.total;
        // the ADD's both-filled entry outcome (dry-run simulated fills) -> APPEND, keep the reservation.
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "a2".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "b2".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        reserve_exposure(&mut exp, &pos, 0.95); // spawn-reserve the add
        pending.insert(pair.slug.clone());
        let reserved_after_two = exp.total;
        let out = SubmitOutcome { slug: pair.slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: sim_ack("a2"), b: sim_ack("b2") }, position: Some(pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert_eq!(positions.lock().unwrap().get(&pair.slug).unwrap().legs.len(), 2, "the dry-run add appended a 2nd leg");
        assert!((exp.total - reserved_after_two).abs() < 1e-9, "both-filled keeps the add reservation (no release)");
        assert!(reserved_after_two > reserved_after_one, "the add added exposure");
        assert!(!halt.load(Ordering::Relaxed), "a clean simulated add never halts (dry-run safe)");
    }

    /// TEST 10 (design §6.10 + §5.4): SINGLE-POSITION / SAFE-DEFAULT regression. With the SHIPPED defaults
    /// (both flags FALSE, cap 1), `qualifying_add` ALWAYS returns None for a held slug — so the loop's add
    /// path always `continue`s and behavior is byte-identical to the one-position-per-slug bot. (The full
    /// ~136-test baseline staying green is the rest of this regression.)
    #[test]
    fn test10_safe_defaults_admit_zero_adds() {
        let def = crate::config::Config::test_default(); // enable_scale_in=false, enable_reentry=false, cap=1
        assert!(!def.enable_scale_in && !def.enable_reentry && def.max_positions_per_slug == 1, "shipped defaults are safe");
        let held = [held_leg(0.03, Dir::PK)];
        // a hugely-bigger same-direction arb, edge live OR closed -> STILL blocked at the defaults.
        let huge = Edge { net: 0.50, dir: Dir::PK };
        assert_eq!(qualifying_add(&def, &held, &huge, true), None, "defaults: SCALE-IN candidate blocked (flag off + cap 1)");
        assert_eq!(qualifying_add(&def, &held, &huge, false), None, "defaults: RE-ENTRY candidate blocked (flag off + cap 1)");
        // the from_env defaults match (the real shipped config, not just test_default).
        // (env is process-global; we assert the constants the from_env literals use instead of mutating env.)
        assert_eq!(qualifying_add(&add_cfg(false, false, 1), &held, &huge, true), None);
    }

    /// W-1 REGRESSION (the independent review's R4-A repro, ASSERTION FLIPPED to halt==true). An armed
    /// scale-in/re-entry can have an ADD in flight on a held slug when a postponement fires. `spawn_unwind`
    /// marks the slug `flattening` (an UNWIND of the BASE leg `legs[0]`). If the ADD then lands HALF-FILLED,
    /// `recover_naked_leg` USED to see `flattening.contains(slug)` and return true ("already covered") — but
    /// the in-flight UNWIND covers `legs[0]`, NOT the add's freshly-naked leg, so that add leg was abandoned
    /// as a SILENT live unhedged position (the W-1 bug: spawned_recovery=false, halted=false). The fix records
    /// WHICH kind holds the slot; an UNWIND-held slot is NOT "covered" for a recovery -> FAIL CLOSED (halt).
    /// The invariant this guards: a filled leg is NEVER left without EITHER a fired recovery OR a halt.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn w1_halffilled_add_while_unwind_in_flight_fails_closed() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // a BASE leg is held; a postpone-unwind of it is already IN FLIGHT (slug marked Unwind, targeting
        // legs[0]). This is exactly the state `spawn_unwind` leaves once it fires the base leg's two SELLs.
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        flat.insert(slug.clone(), FlatKind::Unwind);
        // the in-flight ADD is spawn-reserved on top, then lands HALF-FILLED (leg A live, leg B errored).
        let add_legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "add-a".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "add-b".into() },
        ];
        let add_pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs);
        reserve_exposure(&mut exp, &add_pos, 0.95);
        pending.insert(slug.clone());
        // CRUCIAL: the pmus book for the add's filled leg IS priceable (a YES bid), so the add leg COULD be
        // flattened in isolation. The bot must STILL halt — a recovery here would race the unwind's pop of
        // legs[0] through the single per-slug flatten slot, so fail-closed is the only safe outcome.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.06, 500.0)], &[(0.08, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        // the unfilled leg is a CLEAN FOK-miss (Ok, not filled) — so it reaches the W-1 unwind-slot check
        // rather than the 2026-06-15 ambiguous-Err early fail-close (which is exercised separately below).
        let resting = || Ok(exec::Ack { client_order_id: "add-b".into(), venue_order_id: "PM-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("add-a"), b: resting() }, position: Some(add_pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // THE FIX: the add's naked leg is NOT silently abandoned — the bot FAIL-CLOSES (halt). (Pre-fix this
        // was halt==false, the abandon.) The add's reservation is still released; the base position + its
        // in-flight unwind slot are untouched (the unwind still owns legs[0]); no SECOND flatten was spawned.
        assert!(halt.load(Ordering::Relaxed), "W-1: a half-filled add racing an unwind must FAIL-CLOSE, never silently abandon");
        assert!((exp.total - 0.90 * 3.0).abs() < 1e-9, "the add's reservation is released; the base reservation is intact");
        assert_eq!(exp.open_positions, 1, "back to the one held BASE position");
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "the base leg is intact (the add never recorded a leg)");
        assert_eq!(flat.get(&slug), Some(&FlatKind::Unwind), "the in-flight UNWIND still owns the flatten slot (recovery did NOT clobber it)");
        assert!(rx.try_recv().is_err(), "no recovery flatten was spawned (no competing SELL races the unwind's legs[0] pop)");

        // SAFE BRANCH 1: a slot held by a prior RECOVERY for THIS slug's naked leg DOES short-circuit (the
        // genuine "already covered" — the in-flight recovery flattens this very leg; don't double-fire).
        let mut flat_rec: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat_rec.insert(slug.clone(), FlatKind::Recovery);
        let (tx2, _rx2) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let covered = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat_rec, &tx2,
            &slug, &exec::PairAck { a: live_ack("x"), b: resting() }, Some(&position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs)));
        assert!(covered, "a recovery-held slot is genuinely covered -> recover_naked_leg returns true (no double-fire, no halt)");

        // SAFE BRANCH 2 (mirror of the bug, isolated): an UNWIND-held slot returns FALSE so the caller halts.
        let mut flat_unw: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat_unw.insert(slug.clone(), FlatKind::Unwind);
        let (tx3, _rx3) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let covered_unw = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat_unw, &tx3,
            &slug, &exec::PairAck { a: live_ack("x"), b: resting() }, Some(&position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs)));
        assert!(!covered_unw, "an unwind-held slot does NOT cover a fresh naked leg -> false -> caller fail-closes");
    }

    /// AMBIGUOUS-LEG FAIL-CLOSE (2026-06-15): when the UNFILLED leg ERR'd (a 4xx/transport error, fate
    /// UNKNOWN — it may have FILLED), `recover_naked_leg` must FAIL CLOSED (return false -> caller halts)
    /// EVEN when the filled leg's book IS priceable. Flattening the known-filled leg while the "unfilled" one
    /// might also have filled would un-hedge a real LOCK. A clean Ok-but-resting miss (FOK kill) still
    /// recovers — contrast asserted here so the gate is by ERR, not by "not filled".
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn errd_unfilled_leg_fails_closed_even_with_priceable_book() {
        let pair = wx_pair();
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        let slug = pos.market.clone();
        // a PRICEABLE pmus book for the filled leg — so an Ok-resting miss WOULD recover; an Err must NOT.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.06, 500.0)], &[(0.08, 500.0)]);
        pmus_books.insert(slug.clone(), pb);

        // leg A (pmus) filled LIVE; leg B (Kalshi) ERR'd -> fate unknown -> FAIL CLOSED (false), no flatten spawned.
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let errd = exec::PairAck { a: live_ack("a"), b: Err(exec::ExecError::Rejected("400 bad".into())) };
        let launched = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat, &tx, &slug, &errd, Some(&pos));
        assert!(!launched, "an ERR'd unfilled leg (fate unknown) must fail closed -> false -> caller halts");
        assert!(!flat.contains_key(&slug), "no recovery flatten was marked (could un-hedge a possible lock)");
        assert!(rx.try_recv().is_err(), "no recovery SELL was spawned for an ambiguous-Err naked outcome");

        // CONTRAST: the same priceable book + an Ok-but-resting (clean FOK miss) DOES recover -> true.
        let mut flat2: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let (tx2, _rx2) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let resting = Ok(exec::Ack { client_order_id: "b".into(), venue_order_id: "K-2".into(), filled: false, simulated: false });
        let ok_miss = exec::PairAck { a: live_ack("a"), b: resting };
        let launched2 = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat2, &tx2, &slug, &ok_miss, Some(&pos));
        assert!(launched2, "a clean Ok-but-resting miss (not an Err) still recovers when the book is priceable");
        assert_eq!(flat2.get(&slug), Some(&FlatKind::Recovery), "the clean-miss case marks the slug for recovery");
    }
}
