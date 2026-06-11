//! Venue request signing — ports `scripts/verify_pmus_auth.py` (pmus Ed25519) and
//! `bot/kalshi_book.py::kalshi_ws_headers` (Kalshi RSA-PSS) to Rust. Both sign the canonical string
//! `"{ts_ms}{METHOD}{path}"`; the timestamp is millis since epoch. Keys load from external paths/env
//! (never embedded). These produce the auth headers for both the WS handshakes and REST orders.
//!
//! Signature correctness is validated by round-trip (sign -> verify) AND acceptance is VERIFIED live
//! (2026-06-11): the Kalshi RSA-PSS headers were accepted on demo + prod (signed balance reads + a live
//! placed/cancelled order) and the pmus Ed25519 headers on GET + POST (proven by a bad-sig->401 vs
//! good-body-less-sig->past-auth control). The scheme matches the Python byte-for-byte (PSS salt = the
//! SHA-256 digest length, 32).

use base64::{engine::general_purpose::STANDARD, Engine as _};

/// The canonical string both venues sign: `"{ts_ms}{METHOD}{path}"`.
pub fn canonical(ts_ms: u128, method: &str, path: &str) -> String {
    format!("{ts_ms}{method}{path}")
}

/// Millis since the Unix epoch — the timestamp both venues require (≤30 s / 5 s skew). Single source so
/// the WS handshake and the REST POST sign with the same clock convention. A pre-epoch clock can't
/// produce a valid signing timestamp (every venue would 401 on the stale string), so fail LOUD rather
/// than silently sign with `0` — a wrong signing clock is unrecoverable, not a transient error.
pub fn now_ms_for_sign() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before UNIX epoch — cannot sign")
        .as_millis()
}

// ----------------------------------------------------------------------------------------------
// polymarket.us — Ed25519
// ----------------------------------------------------------------------------------------------
use ed25519_dalek::{Signer, SigningKey};

/// Load a pmus Ed25519 signing key from the base64 secret (first 32 bytes are the seed).
pub fn pmus_key_from_secret_b64(secret_b64: &str) -> Result<SigningKey, String> {
    let raw = STANDARD
        .decode(secret_b64.trim())
        .map_err(|e| format!("pmus secret base64: {e}"))?;
    let seed: [u8; 32] = raw
        .get(..32)
        .and_then(|s| s.try_into().ok())
        .ok_or_else(|| "pmus secret < 32 bytes".to_string())?;
    Ok(SigningKey::from_bytes(&seed))
}

/// Sign `"{ts}{METHOD}{path}"` with Ed25519 -> base64 signature (the `X-PM-Signature` value).
pub fn pmus_sign(key: &SigningKey, ts_ms: u128, method: &str, path: &str) -> String {
    let sig = key.sign(canonical(ts_ms, method, path).as_bytes());
    STANDARD.encode(sig.to_bytes())
}

/// The signed pmus headers: (access-key, timestamp, signature).
pub fn pmus_headers(key: &SigningKey, access_key: &str, ts_ms: u128, method: &str, path: &str)
    -> [(&'static str, String); 3]
{
    [
        ("X-PM-Access-Key", access_key.to_string()),
        ("X-PM-Timestamp", ts_ms.to_string()),
        ("X-PM-Signature", pmus_sign(key, ts_ms, method, path)),
    ]
}

// ----------------------------------------------------------------------------------------------
// Kalshi — RSA-PSS (SHA-256, salt = digest length 32)
// ----------------------------------------------------------------------------------------------
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey as PssSigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use sha2::Sha256;

/// Load a Kalshi RSA private key from PEM (PKCS#8 `BEGIN PRIVATE KEY` or PKCS#1 `BEGIN RSA PRIVATE KEY`).
pub fn kalshi_key_from_pem(pem: &str) -> Result<RsaPrivateKey, String> {
    RsaPrivateKey::from_pkcs8_pem(pem)
        .or_else(|_| RsaPrivateKey::from_pkcs1_pem(pem))
        .map_err(|e| format!("kalshi rsa key parse: {e}"))
}

/// Sign `"{ts}{METHOD}{path}"` with RSA-PSS(SHA-256, salt=32) -> base64 (the signature header value).
pub fn kalshi_sign(key: &RsaPrivateKey, ts_ms: u128, method: &str, path: &str) -> String {
    // salt length = SHA-256 digest length (32) — matches the Python PSS.DIGEST_LENGTH.
    let signing_key = PssSigningKey::<Sha256>::new_with_salt_len(key.clone(), 32);
    let sig = signing_key.sign_with_rng(&mut rand::thread_rng(), canonical(ts_ms, method, path).as_bytes());
    STANDARD.encode(sig.to_bytes())
}

/// The signed Kalshi headers: (access-key, timestamp, signature).
pub fn kalshi_headers(key: &RsaPrivateKey, access_key: &str, ts_ms: u128, method: &str, path: &str)
    -> [(&'static str, String); 3]
{
    [
        ("KALSHI-ACCESS-KEY", access_key.to_string()),
        ("KALSHI-ACCESS-TIMESTAMP", ts_ms.to_string()),
        ("KALSHI-ACCESS-SIGNATURE", kalshi_sign(key, ts_ms, method, path)),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::Verifier;

    #[test]
    fn canonical_string_matches_python() {
        assert_eq!(canonical(1781139999123, "GET", "/v1/portfolio/positions"),
                   "1781139999123GET/v1/portfolio/positions");
    }

    #[test]
    fn pmus_ed25519_round_trips() {
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let msg = canonical(123, "GET", "/p");
        let b64 = pmus_sign(&key, 123, "GET", "/p");
        let raw = STANDARD.decode(b64).unwrap();
        let sig = ed25519_dalek::Signature::from_slice(&raw).unwrap();
        assert!(key.verifying_key().verify(msg.as_bytes(), &sig).is_ok());
        assert_eq!(pmus_headers(&key, "ak", 123, "GET", "/p")[0].0, "X-PM-Access-Key");
    }

    #[test]
    fn kalshi_rsa_pss_round_trips() {
        // 2048-bit keygen once for this test; validates the full PSS sign->verify pipeline.
        let key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).unwrap();
        let msg = canonical(456, "POST", "/trade-api/v2/portfolio/orders");
        let b64 = kalshi_sign(&key, 456, "POST", "/trade-api/v2/portfolio/orders");
        let raw = STANDARD.decode(b64).unwrap();
        use rsa::pss::{Signature, VerifyingKey};
        use rsa::signature::Verifier as _;
        let vk = VerifyingKey::<Sha256>::new_with_salt_len(key.to_public_key(), 32);
        let sig = Signature::try_from(raw.as_slice()).unwrap();
        assert!(vk.verify(msg.as_bytes(), &sig).is_ok());
        assert_eq!(kalshi_headers(&key, "ak", 456, "POST", "/x")[0].0, "KALSHI-ACCESS-KEY");
    }
}
