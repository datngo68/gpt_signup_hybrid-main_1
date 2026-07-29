//! FIFO job queue + worker pool với hard cap, per-job timeout, cancel token.
//!
//! Khi N worker đầy → job mới xếp hàng trong channel buffer (size =
//! `queue_capacity`). Đầy nữa → `try_submit` reject với `SubmitError::QueueFull`.
//!
//! Mỗi job có:
//!   - `timeout` cứng (`run_upi_qr` quá deadline → kill, free worker).
//!   - `CancellationToken` từ `JobRegistry` — `/stop` của user trigger cancel
//!     ngay lập tức kể cả khi đang trong sleep/IO.

use crate::http::HttpClient;
use crate::upi::runner::{run_upi_qr, UpiJobConfig};
use crate::upi::types::UpiQrResult;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, Mutex, Semaphore};
use tokio_util::sync::CancellationToken;

pub struct Job {
    pub user_id: i64,
    pub job_id: u64,
    #[allow(dead_code)]
    pub chat_id: i64,
    #[allow(dead_code)]
    pub username: Option<String>,
    pub config: UpiJobConfig,
    pub log_tx: mpsc::UnboundedSender<JobEvent>,
    /// Token từ `JobRegistry::register`. Khi user `/stop` → cancel.
    pub cancel: CancellationToken,
}

#[derive(Debug, Clone)]
pub enum JobEvent {
    #[allow(dead_code)]
    Queued { position: usize },
    Started,
    Log(String),
    Done(UpiQrResult),
    Timeout,
    Cancelled,
}

#[derive(Debug, thiserror::Error)]
pub enum SubmitError {
    #[error("queue full ({pending}/{capacity}) — RAM safeguard")]
    QueueFull { pending: usize, capacity: usize },
    #[error("queue closed")]
    Closed,
}

pub struct JobQueue {
    submit_tx: mpsc::Sender<Job>,
    capacity: usize,
}

impl JobQueue {
    pub fn new(capacity: usize) -> (Self, mpsc::Receiver<Job>) {
        let (tx, rx) = mpsc::channel::<Job>(capacity.max(1));
        (
            Self {
                submit_tx: tx,
                capacity,
            },
            rx,
        )
    }

    pub fn pending(&self) -> usize {
        self.capacity.saturating_sub(self.submit_tx.capacity())
    }

    pub fn try_submit(&self, job: Job) -> Result<usize, SubmitError> {
        let pending_before = self.pending();
        match self.submit_tx.try_send(job) {
            Ok(()) => Ok(pending_before + 1),
            Err(mpsc::error::TrySendError::Full(_)) => Err(SubmitError::QueueFull {
                pending: pending_before,
                capacity: self.capacity,
            }),
            Err(mpsc::error::TrySendError::Closed(_)) => Err(SubmitError::Closed),
        }
    }
}

pub struct WorkerConfig {
    pub max_concurrent: usize,
    pub job_timeout: Duration,
}

/// Spawn worker pool. Mỗi worker khi có job:
///   1. Acquire semaphore permit
///   2. Check token chưa bị cancel (queue cancel — user /stop khi job vẫn pending)
///   3. Send `Started` event
///   4. `tokio::select!` chạy run_upi_qr với timeout + cancel
///   5. Send event tương ứng (Done/Timeout/Cancelled)
///   6. Cleanup QR artifacts nếu fail
///   7. Gọi `on_done(user_id, job_id)` để registry/limiter cleanup
pub fn spawn_workers(
    client: Arc<HttpClient>,
    mut rx: mpsc::Receiver<Job>,
    on_done: Arc<dyn Fn(i64, u64) + Send + Sync>,
    cfg: WorkerConfig,
) {
    let sem = Arc::new(Semaphore::new(cfg.max_concurrent));
    let active = Arc::new(Mutex::new(0usize));

    tokio::spawn(async move {
        while let Some(job) = rx.recv().await {
            let sem = sem.clone();
            let client = client.clone();
            let active = active.clone();
            let on_done = on_done.clone();
            let timeout = cfg.job_timeout;
            tokio::spawn(async move {
                // Cancel trước khi worker pickup → trả Cancelled, không chiếm slot
                if job.cancel.is_cancelled() {
                    let _ = job.log_tx.send(JobEvent::Cancelled);
                    cleanup_qr_file(&job.config.qr_out_path);
                    on_done(job.user_id, job.job_id);
                    return;
                }

                let permit = match sem.acquire_owned().await {
                    Ok(p) => p,
                    Err(_) => return,
                };
                {
                    let mut g = active.lock().await;
                    *g += 1;
                    tracing::info!(
                        active = *g,
                        user_id = job.user_id,
                        job_id = job.job_id,
                        "worker start"
                    );
                }
                let _ = job.log_tx.send(JobEvent::Started);
                let log_tx = job.log_tx.clone();
                let log_fn: crate::upi::runner::LogFn = Arc::new(move |line: &str| {
                    let _ = log_tx.send(JobEvent::Log(line.to_string()));
                });

                let user_id = job.user_id;
                let job_id = job.job_id;
                let qr_out = job.config.qr_out_path.clone();
                let cancel_for_run = job.cancel.clone();

                let outcome = tokio::select! {
                    biased;
                    _ = cancel_for_run.cancelled() => Outcome::Cancelled,
                    res = tokio::time::timeout(timeout, run_upi_qr(client, job.config, log_fn)) => {
                        match res {
                            Ok(r) => Outcome::Done(r),
                            Err(_) => Outcome::Timeout,
                        }
                    }
                };

                match outcome {
                    Outcome::Done(result) => {
                        let _ = job.log_tx.send(JobEvent::Done(result));
                    }
                    Outcome::Timeout => {
                        tracing::warn!(
                            user_id,
                            job_id,
                            timeout_secs = timeout.as_secs(),
                            "job timeout — killed"
                        );
                        let _ = job.log_tx.send(JobEvent::Timeout);
                        cleanup_qr_file(&qr_out);
                    }
                    Outcome::Cancelled => {
                        tracing::info!(user_id, job_id, "job cancelled by user");
                        let _ = job.log_tx.send(JobEvent::Cancelled);
                        cleanup_qr_file(&qr_out);
                    }
                }

                drop(permit);
                {
                    let mut g = active.lock().await;
                    *g = g.saturating_sub(1);
                    tracing::info!(active = *g, user_id, job_id, "worker done");
                }
                on_done(user_id, job_id);
            });
        }
        tracing::warn!("queue closed, workers exit");
    });
}

enum Outcome {
    Done(UpiQrResult),
    Timeout,
    Cancelled,
}

fn cleanup_qr_file(path: &PathBuf) {
    if path.exists() {
        if let Err(e) = std::fs::remove_file(path) {
            tracing::debug!("cleanup_qr_file {} fail: {}", path.display(), e);
        }
    }
    for ext in ["html", "svg"] {
        let p = path.with_extension(ext);
        if p.exists() {
            let _ = std::fs::remove_file(p);
        }
    }
}

pub fn cleanup_qr_artifacts(path: &PathBuf) {
    cleanup_qr_file(path);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn queue_full_rejects() {
        let (q, _rx) = JobQueue::new(2);
        let mk = || -> Job {
            let (tx, _rx) = mpsc::unbounded_channel();
            Job {
                user_id: 0,
                job_id: 0,
                chat_id: 0,
                username: None,
                config: UpiJobConfig {
                    email: "x".into(),
                    auth: crate::upi::runner::AuthSource::Session {
                        access_token: "x".into(),
                        cookie_header: "".into(),
                    },
                    proxy_pool: vec![],
                    approve_retries: 1,
                    approve_delay_ms: None,
                    restart_threshold: 0,
                    max_restarts: 0,
                    proxy_from_step: 3,
                    login_proxy: None,
                    qr_out_path: PathBuf::from("/tmp/x.png"),
                    bundles_cache_dir: PathBuf::from("/tmp/x"),
                    qr_watermark: String::new(),
                },
                log_tx: tx,
                cancel: CancellationToken::new(),
            }
        };
        assert!(q.try_submit(mk()).is_ok());
        assert!(q.try_submit(mk()).is_ok());
        match q.try_submit(mk()) {
            Err(SubmitError::QueueFull { pending, capacity }) => {
                assert_eq!(capacity, 2);
                assert!(pending >= 2);
            }
            other => panic!("expected QueueFull, got {:?}", other),
        }
    }
}
