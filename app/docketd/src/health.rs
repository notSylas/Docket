//! Worker health cache and its background refresh.
//!
//! `GET /v1/status` never calls the worker: it reads this cache. A background
//! task asks the worker for `health` only while no question is running and the
//! worker is up, with a short call timeout. A question is never refused or
//! cancelled for a refresh (the refresh does not take the ask permit); the only
//! interaction is that a question arriving during a refresh queues behind that
//! one call on the supervisor channel for at most its (short) duration:
//! the health timeout (default 2 s). That bound is the whole cost to a
//! question, because the health call uses a SOFT timeout: missing it does not
//! kill or replace the worker (see `CallOpts::soft`), it only marks the
//! worker-derived sections stale with `refresh_error_code = health_timeout`.
//! `responsiveness` becomes `unresponsive` only through the existing ping
//! hang detection (no successful exchange within the stale window).
//!
//! Freshness: every section carries the producer's `checked_at_ms`; the daemon
//! computes `age_ms` and `stale` when the response is built. A section is
//! stale when it is older than the threshold, when its worker process is gone
//! or has been replaced, or when the producer gave no timestamp. Cached data is
//! never erased on a worker failure and never shown as current.
//!
//! The `worker_process` section is computed by the daemon itself from the
//! supervisor's own bookkeeping, so it stays fresh while the worker is busy or
//! down.

use crate::config::Config;
use crate::worker::{CallError, CallOpts, WorkerHandle, WorkerMeta};
use docket_proto::api::{WorkerInfo, WorkerState};
use docket_proto::status::{Responsiveness, Section, StatusHealth, WorkerHealth, WorkerProcess};
use serde_json::Value;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::{watch, Semaphore};
use tracing::{debug, warn};

pub const FEATURE: &str = "health.v1";

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Default)]
struct Inner {
    health: Option<WorkerHealth>,
    /// Worker generation that produced `health`.
    generation: u64,
    skipped_busy: u64,
    error_code: Option<&'static str>,
}

#[derive(Clone, Default)]
pub struct HealthCache(Arc<Mutex<Inner>>);

impl HealthCache {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn store(&self, health: WorkerHealth, generation: u64) {
        let mut g = self.lock();
        g.health = Some(health);
        g.generation = generation;
        g.error_code = None;
    }

    fn fail(&self, code: &'static str) {
        self.lock().error_code = Some(code);
    }

    fn skipped(&self) {
        self.lock().skipped_busy += 1;
    }

    fn generation(&self) -> Option<u64> {
        let g = self.lock();
        g.health.as_ref().map(|_| g.generation)
    }

    /// Build the `health` of a status response at `now`.
    pub fn view(
        &self,
        cfg: &Config,
        info: &WorkerInfo,
        meta: &WorkerMeta,
        busy: bool,
        now: u64,
    ) -> StatusHealth {
        let stale_after = cfg.health_stale.as_millis() as u64;
        let g = self.lock();
        let producer_gone = info.state != WorkerState::Up || g.generation != meta.generation;
        let mut sections = g.health.clone().unwrap_or_default();
        // A failed refresh (e.g. `health_timeout`) means what we hold is not
        // current, whatever its timestamp says.
        let producer_gone = producer_gone || g.error_code.is_some();
        fn mark<T>(s: &mut Option<Section<T>>, now: u64, stale_after: u64, gone: bool) {
            if let Some(s) = s {
                let age = s.checked_at_ms.map(|t| now.saturating_sub(t));
                s.age_ms = age;
                s.stale = Some(gone || age.is_none_or(|a| a > stale_after));
            }
        }
        mark(&mut sections.process, now, stale_after, producer_gone);
        mark(&mut sections.database, now, stale_after, producer_gone);
        mark(&mut sections.models, now, stale_after, producer_gone);
        mark(&mut sections.indexing, now, stale_after, producer_gone);
        let last_ok_age = meta.last_ok_ms.map(|t| now.saturating_sub(t));
        let responsiveness = match info.state {
            WorkerState::Down => Responsiveness::Unresponsive,
            WorkerState::Starting => Responsiveness::Unknown,
            WorkerState::Up if busy => Responsiveness::Busy,
            WorkerState::Up => match last_ok_age {
                Some(a) if a <= stale_after => Responsiveness::Responsive,
                Some(_) => Responsiveness::Unresponsive,
                None => Responsiveness::Unknown,
            },
        };
        StatusHealth {
            v: 1,
            worker_process: WorkerProcess {
                state: info.state,
                restarts: info.restarts,
                crashes: meta.crashes,
                since_ms: now.saturating_sub(meta.since_ms),
                last_error_code: meta.last_error_code.map(String::from),
                responsiveness,
                last_ok_age_ms: last_ok_age,
                checked_at_ms: now,
            },
            refresh_interval_ms: cfg.health_interval.as_millis() as u64,
            stale_after_ms: stale_after,
            refresh_skipped_busy: g.skipped_busy,
            refresh_error_code: g.error_code.map(String::from),
            sections,
        }
    }
}

fn error_code(e: &CallError) -> &'static str {
    match e {
        CallError::Timeout => "health_timeout",
        CallError::Crashed => "worker_crashed",
        CallError::Unavailable(_) => "worker_unavailable",
        CallError::Rpc(_) => "worker_error",
        CallError::Cancelled { .. } => "cancelled",
    }
}

/// Refresh the cache until `shutdown`. Does nothing for a worker that does not
/// advertise `health.v1` (an old worker is never sent a `health` call).
pub async fn refresh_loop(
    cfg: Config,
    worker: WorkerHandle,
    ask_slot: Arc<Semaphore>,
    cache: HealthCache,
    mut shutdown: watch::Receiver<bool>,
) {
    if cfg.health_interval.is_zero() {
        return;
    }
    let poll = cfg.health_interval.min(Duration::from_millis(250));
    let mut last_attempt: Option<Instant> = None;
    let mut tick = tokio::time::interval(poll);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = shutdown.changed() => return,
            _ = tick.tick() => {}
        }
        let meta = worker.meta();
        if !meta.features.iter().any(|f| f == FEATURE) || worker.info().state != WorkerState::Up {
            continue;
        }
        let due = last_attempt.is_none_or(|t| t.elapsed() >= cfg.health_interval)
            || cache.generation() != Some(meta.generation);
        if !due {
            continue;
        }
        if ask_slot.available_permits() == 0 {
            cache.skipped();
            // Check again soon: refresh resumes right after the question ends.
            continue;
        }
        last_attempt = Some(Instant::now());
        match worker
            .call("health", Value::Null, CallOpts::soft(cfg.health_timeout))
            .await
        {
            Ok(v) => match serde_json::from_value::<WorkerHealth>(v) {
                Ok(mut h) => {
                    h.sanitize();
                    // Drop a result that raced a worker replacement.
                    if worker.meta().generation == meta.generation {
                        cache.store(h, meta.generation);
                    }
                }
                Err(_) => cache.fail("bad_worker_response"),
            },
            Err(e) => {
                debug!(target: "docketd::health", code = error_code(&e), "health refresh failed");
                if matches!(e, CallError::Timeout | CallError::Crashed) {
                    warn!(target: "docketd::health", code = error_code(&e), "worker missed its health call");
                }
                cache.fail(error_code(&e));
            }
        }
    }
}
