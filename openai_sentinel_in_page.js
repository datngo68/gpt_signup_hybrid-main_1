// openai_sentinel_in_page.js
//
// Page-native sentinel-token generator. Runs INSIDE a Camoufox page via
// ``page.evaluate(...)``. Unlike ``openai_sentinel_quickjs.js`` which mocks
// window/document/navigator inside Node/QuickJS, this version relies on the
// REAL browser environment so ``sdk.js`` reads real canvas/WebGL/audio
// fingerprints — exactly what OpenAI's server expects from a legitimate
// Firefox/Chrome user. QuickJS mocks return undefined/empty for those
// vectors → server-side anomaly detector flags the account → deferred ban.
//
// Protocol mirrors ``openai_sentinel_quickjs.js``:
//   requirements -> { request_p }
//   solve        -> { final_p, t }
//
// Caller (sentinel_browser.SentinelBrowserOracle) handles HTTP POST to
// ``/sentinel/req`` via ``ctx.request`` (preserves cookies + TLS fingerprint).
//
// Async wrapper invoked as ``await page.evaluate(<this script string>, args)``.

async function __runSentinelInPage(args) {
  // Same patch markers as openai_sentinel_quickjs.js. Keep in sync if sdk.js
  // is rotated to a new version.
  const SDK_GLOBAL_PATCH = "var SentinelSDK=";
  const SDK_GLOBAL_REPLACEMENT = "globalThis.SentinelSDK=";
  const INSTANCE_PATCH = "var P=new _;";
  const INSTANCE_REPLACEMENT = "var P=new _;globalThis.__debugP=P;";
  const EXPOSE_PATCH =
    "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});";
  const EXPOSE_REPLACEMENT =
    "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t.__debug_n=_n,t.__debug_bindProof=D,t}({});";

  // One-time load — cache via globalThis so subsequent page.evaluate() calls
  // reuse the same SentinelSDK instance (preserves storage + so-token state).
  if (!globalThis.__debugP) {
    let sdk = String(args.sdkSource || "");
    if (!sdk) throw new Error("missing sdkSource");
    let patched = 0;
    const originalLen = sdk.length;
    sdk = sdk.replace(SDK_GLOBAL_PATCH, () => { patched += 1; return SDK_GLOBAL_REPLACEMENT; });
    sdk = sdk.replace(INSTANCE_PATCH, () => { patched += 1; return INSTANCE_REPLACEMENT; });
    sdk = sdk.replace(EXPOSE_PATCH, () => { patched += 1; return EXPOSE_REPLACEMENT; });
    if (patched < 3) {
      throw new Error(
        "sdk.js patch failed: only " + patched + "/3 markers replaced " +
        "(sdk version likely changed; update patch markers)"
      );
    }
    // Use indirect ``(0, eval)`` to evaluate in the global scope so that
    // ``var SentinelSDK`` -> ``globalThis.SentinelSDK`` assignment lands on
    // the real window, not the wrapper function scope.
    (0, eval)(sdk);
    if (typeof globalThis.__debugP !== "object" || globalThis.__debugP === null) {
      throw new Error("sdk.js loaded but __debugP not exposed");
    }
    if (typeof globalThis.SentinelSDK !== "object" || globalThis.SentinelSDK === null) {
      throw new Error("sdk.js loaded but SentinelSDK not exposed");
    }
  }

  const payload = args.payload || {};
  const action = String(payload.action || "").trim();

  if (action === "requirements") {
    const requestP = await globalThis.__debugP.getRequirementsToken();
    return { request_p: String(requestP || "") };
  }

  if (action === "solve") {
    const challenge = payload.challenge || {};
    const requestP = String(payload.request_p || "").trim();
    if (!requestP) throw new Error("missing request_p");
    const finalP = await globalThis.__debugP.getEnforcementToken(challenge);
    globalThis.SentinelSDK.__debug_bindProof(challenge, requestP);
    const dx = challenge && challenge.turnstile ? challenge.turnstile.dx : null;
    const tValue = dx
      ? await globalThis.SentinelSDK.__debug_n(challenge, dx)
      : null;
    return { final_p: String(finalP || ""), t: tValue == null ? null : String(tValue) };
  }

  throw new Error("unsupported action: " + action);
}
