use crate::pair::{lock, pair_tickers, LivePair, PairState};
use crate::{book, discovery, postpone, venue};

/// Log the discovery coverage report LOUDLY (L7): an unmapped category / misaligned bucket / truncated
/// catalog must never pass silently — a human decides whether to extend the config.
pub(crate) fn report_coverage(d: &discovery::Discovery) {
    if !d.weather_cities_unmapped.is_empty() {
        println!("[coverage] UNMAPPED climate cities (MISSED until added to discovery::WX): {:?}", d.weather_cities_unmapped);
    }
    if !d.sports_leagues_unmapped.is_empty() {
        println!("[coverage] UNMAPPED sports leagues: {:?}", d.sports_leagues_unmapped);
    }
    if !d.soccer_leagues_unmapped.is_empty() {
        println!("[coverage] UNMAPPED soccer (drawable-outcome) leagues: {:?}", d.soccer_leagues_unmapped);
    }
    for (slug, why) in &d.soccer_unbound {
        // LOUD, never silent: a WC game whose codes mismatch AND whose names don't agree -> would be missed.
        println!("[wc-unbound] no Kalshi bind (code+name both failed): {slug}  ({why})");
    }
    if d.weather_buckets_misaligned > 0 {
        println!("[coverage] {} weather buckets had no identical-bounds Kalshi twin (NOT paired)", d.weather_buckets_misaligned);
    }
    if d.truncated {
        println!("[coverage] WARNING pmus catalog hit the page cap — coverage INCOMPLETE");
    }
}

/// Set-diff the CURRENT subscribed keys against a FRESH discovery's keys -> the in-place `SubUpdate`
/// (add = fresh-not-current; del = current-not-fresh). Pure; the prune debounce is applied separately so
/// a transient discovery blip can't drop a live market (see `prune_step`).
pub(crate) fn diff_targets(current: &std::collections::HashSet<String>, fresh: &std::collections::HashSet<String>, to_prune: &std::collections::HashSet<String>) -> venue::SubUpdate {
    let add = fresh.iter().filter(|k| !current.contains(*k)).cloned().collect();
    let del = to_prune.iter().filter(|k| current.contains(*k)).cloned().collect();
    venue::SubUpdate { add, del }
}

/// 2-miss prune debounce (port of `monitor.py::prune_decision`): a tracked slug absent from `current`
/// discovery for `threshold` consecutive refreshes is settled -> prune. A reappearance resets its count,
/// so an API hiccup / pagination blip doesn't tear down a still-live market. Mutates `absent`.
pub(crate) fn prune_step(
    tracked: &std::collections::HashSet<String>,
    current: &std::collections::HashSet<String>,
    absent: &mut std::collections::HashMap<String, u32>,
    threshold: u32,
) -> std::collections::HashSet<String> {
    let mut to_prune = std::collections::HashSet::new();
    for slug in tracked {
        if current.contains(slug) {
            absent.insert(slug.clone(), 0);
        } else {
            let c = absent.entry(slug.clone()).or_insert(0);
            *c += 1;
            if *c >= threshold {
                to_prune.insert(slug.clone());
            }
        }
    }
    to_prune
}

/// PERIODIC RE-DISCOVERY (mirrors `monitor.py::rest_heartbeat`): every `refresh_s` re-pull both catalogs,
/// add newly-listed pairs (no-gap in-place subscribe), and prune settled ones (2-miss debounce). A
/// DEGRADED pull (error) is skipped entirely — never prune on a failed pull (a fetch error makes live
/// markets look settled; monitor.py H4). Supervised: one bad cycle logs and continues, never kills the task.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn refresh_loop(
    http: reqwest::Client,
    refresh_s: u64,
    pairs: std::sync::Arc<std::sync::Mutex<PairState>>,
    k_tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    pm_tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    kalshi_books: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    positions: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    k_subs: tokio::sync::mpsc::UnboundedSender<venue::SubUpdate>,
    pm_subs: tokio::sync::mpsc::UnboundedSender<venue::SubUpdate>,
) {
    use std::collections::{HashMap, HashSet};
    let mut absent: HashMap<String, u32> = HashMap::new(); // pmus slug -> consecutive-miss count
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(refresh_s.max(1))).await;
        let fresh = match discovery::discover(&http).await {
            Ok(d) => d,
            Err(e) => {
                println!("[refresh] discovery DEGRADED ({e}) — keeping current set, no prune (H4)");
                continue;
            }
        };
        report_coverage(&fresh);

        // C9: a TRUNCATED catalog (pmus paging hit the cap) means an absent slug may just be off the cut
        // page, not settled — treat it like a degraded pull FOR PRUNING ONLY: still apply the adds, but skip
        // the prune step this cycle so a live market isn't torn down because the catalog was incomplete.
        let prune_ok = !fresh.truncated;
        if !prune_ok {
            println!("[refresh] pmus catalog TRUNCATED — no prune this cycle");
        }

        // fresh keys by venue. A sports pair contributes BOTH Kalshi tickers (team A + B).
        let fresh_slugs: HashSet<String> = fresh.pairs.iter().map(|p| p.slug.clone()).collect();
        let fresh_tickers: HashSet<String> = fresh.pairs.iter().flat_map(pair_tickers).collect();

        // snapshot the PRE-refresh subscribed set (slugs + each pair's Kalshi ticker(s)) before mutating.
        let (pre_slugs, slug_to_tickers): (HashSet<String>, HashMap<String, Vec<String>>) = {
            let ps = lock(&pairs);
            (
                ps.by_slug.keys().cloned().collect(),
                ps.by_slug.iter().map(|(s, p)| (s.clone(), p.kalshi_tickers())).collect(),
            )
        };
        let pre_tickers: HashSet<String> = slug_to_tickers.values().flatten().cloned().collect();

        // PRUNE debounce (keyed on the pmus slug = the pair identity). W16: never prune a slug with any open
        // held leg — it must stay subscribed/flattenable until closed (else its Kalshi book is freed and the
        // unwind can never price the exit -> an un-flattenable held position). A slug is "held" iff it has a
        // map entry (R5: the key is dropped only when its last `HeldLeg` is flattened), so `keys()` is exactly
        // the held set. A genuinely-settled held slug still accrues misses in `absent` (the debounce runs), so
        // once its last leg closes the next cycle prunes it immediately. C9: on a TRUNCATED catalog, skip the
        // debounce entirely (an off-page slug must not count as a miss) — still apply the adds below.
        let held: HashSet<String> = lock(&positions).keys().cloned().collect();
        let prune_slugs: HashSet<String> = if prune_ok {
            prune_step(&pre_slugs, &fresh_slugs, &mut absent, 2)
                .into_iter()
                .filter(|s| !held.contains(s))
                .collect()
        } else {
            HashSet::new()
        };
        let prune_tickers: HashSet<String> = prune_slugs.iter().filter_map(|s| slug_to_tickers.get(s)).flatten().cloned().collect();

        // the in-place WIRE updates: add = fresh keys not already subscribed; del = the pruned keys. Pure
        // set-diff (the stream tolerates a re-add as a harmless no-gap merge, but we send the minimal set).
        let k_update = diff_targets(&pre_tickers, &fresh_tickers, &prune_tickers);
        let pm_update = diff_targets(&pre_slugs, &fresh_slugs, &prune_slugs);

        // apply to the shared pair map + tracked sets (streams re-subscribe `tracked` on reconnect, so
        // mutate it before dispatching so a reconnect-during-refresh stays consistent).
        let mut added = 0usize;
        {
            let mut ps = lock(&pairs);
            let mut kt = lock(&k_tracked);
            let mut pt = lock(&pm_tracked);
            for p in &fresh.pairs {
                if !ps.by_slug.contains_key(&p.slug) {
                    for tk in pair_tickers(p) {
                        kt.insert(tk); // sports adds BOTH team tickers
                    }
                    pt.insert(p.slug.clone());
                    ps.insert(LivePair::from(p.clone()));
                    added += 1;
                }
            }
            for s in &prune_slugs {
                ps.remove(s);
                pt.remove(s);
                absent.remove(s);
            }
            for tk in &prune_tickers {
                kt.remove(tk);
            }
        }
        // free settled Kalshi books so memory stays FLAT over a multi-week run (L20), not only on the next
        // reconnect-clear. (pmus books are freed in the event loop when an untracked frame arrives.)
        if !prune_tickers.is_empty() {
            let mut kb = lock(&kalshi_books);
            for tk in &prune_tickers {
                kb.remove(tk);
            }
        }

        // dispatch the wire updates (pmus `del` is a local-only no-op — see pmus_stream docs).
        if !k_update.add.is_empty() || !k_update.del.is_empty() {
            let _ = k_subs.send(k_update);
        }
        if !pm_update.add.is_empty() {
            let _ = pm_subs.send(venue::SubUpdate { add: pm_update.add, del: Vec::new() });
        }
        if added > 0 || !prune_slugs.is_empty() {
            println!(
                "[refresh] +{added} pairs, -{} settled ({} weather + {} econ pairs live)",
                prune_slugs.len(), fresh.weather_pairs, fresh.econ_pairs
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};
    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    /// The refresh diff: add = fresh-not-current, del = the pruned keys. Pure set-diff.
    #[test]
    fn diff_targets_adds_new_and_deletes_pruned() {
        let current = set(&["A", "B", "C"]);
        let fresh = set(&["B", "C", "D"]); // A gone, D new
        let to_prune = set(&["A"]); // A debounced out
        let u = diff_targets(&current, &fresh, &to_prune);
        assert_eq!(u.add, vec!["D".to_string()]);
        assert_eq!(u.del, vec!["A".to_string()]);
        // a key still in fresh is never deleted even if it appears in to_prune (defensive: prune wins
        // only on keys actually absent from fresh, which the debounce already guarantees).
        let u2 = diff_targets(&current, &fresh, &HashSet::new());
        assert!(u2.del.is_empty() && u2.add == vec!["D".to_string()]);
    }

    /// The 2-miss prune debounce (monitor.py parity): missing ONCE holds; missing TWICE prunes; a
    /// reappearance resets the miss count.
    #[test]
    fn prune_step_debounces_two_misses() {
        let tracked = set(&["a", "b", "c"]);
        let mut absent: HashMap<String, u32> = HashMap::new();
        // round 1: b,c missing once -> hold (only a is present).
        assert_eq!(prune_step(&tracked, &set(&["a"]), &mut absent, 2), HashSet::new());
        // round 2: still missing -> prune both.
        assert_eq!(prune_step(&tracked, &set(&["a"]), &mut absent, 2), set(&["b", "c"]));
        // a reappearance resets the counter (b back -> not pruned next miss).
        let mut absent2: HashMap<String, u32> = HashMap::new();
        prune_step(&tracked, &set(&["a", "c"]), &mut absent2, 2); // b missing once
        prune_step(&tracked, &set(&["a", "b", "c"]), &mut absent2, 2); // b back -> reset
        assert_eq!(prune_step(&tracked, &set(&["a", "c"]), &mut absent2, 2), HashSet::new()); // b missing once again -> hold
    }
}
