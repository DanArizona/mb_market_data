/* Browser-side confirmation for the *viewer-only* Stop server action.
 * The historical Dash callback can acknowledge "Stopping…" before its WSGI
 * listener has exited. Once the listener is unreachable, the loaded page is
 * still present in Firefox. Show an explicit post-shutdown screen instead of
 * leaving a misleading, forever-spinning "Stopping…" label.
 *
 * No endpoint writes, app controls, credentials, or production APIs are used.
 * A HEAD request verifies the same-origin local viewer listener only.
 */
(() => {
  "use strict";
  let monitoring = false;
  const STOP_STATUS_ID = "server-stop-status";
  const PING_INTERVAL_MS = 450;
  const MAX_WAIT_MS = 12000;
  const FAILURES_NEEDED = 2;

  function showStopped() {
    document.title = "MasterBot Overlay — viewer stopped";
    const overlay = document.createElement("main");
    overlay.id = "mb-viewer-stopped";
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483647",
      "display:flex", "align-items:center", "justify-content:center",
      "padding:24px", "background:#071623", "color:#e0f1f3",
      "font-family:system-ui,Segoe UI,sans-serif", "text-align:center"
    ].join(";");
    const box = document.createElement("div");
    box.style.cssText = "max-width:650px;line-height:1.6";
    const heading = document.createElement("h1");
    heading.textContent = "Overlay viewer stopped";
    heading.style.cssText = "font-size:24px;margin:0 0 12px";
    const note = document.createElement("p");
    note.textContent =
      "The local Overlay server is no longer reachable. You can close this browser tab.";
    const independent = document.createElement("p");
    independent.textContent =
      "The daily Uni / Focus / Hot polling controller is separate and is not stopped by this button.";
    independent.style.cssText = "color:#9ab3c1;font-size:14px";
    box.append(heading, note, independent);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }

  async function watchStopStatus() {
    let failures = 0;
    const began = Date.now();
    const span = document.getElementById(STOP_STATUS_ID);
    if (!span) return;

    async function check() {
      try {
        // A response of any HTTP status still proves a listener is present.
        await fetch("/?_mb_viewer_stop_probe=" + Date.now(), {
          method: "HEAD", cache: "no-store"
        });
        failures = 0;
      } catch (_error) {
        failures += 1;
        if (failures >= FAILURES_NEEDED) {
          showStopped();
          return;
        }
      }
      if (Date.now() - began > MAX_WAIT_MS) {
        span.textContent =
          "Shutdown not confirmed. Inspect the viewer CMD window and port; " +
          "production polling remains independent.";
        return;
      }
      window.setTimeout(check, PING_INTERVAL_MS);
    }
    window.setTimeout(check, PING_INTERVAL_MS);
  }

  function detectStopRequest() {
    if (monitoring) return;
    const span = document.getElementById(STOP_STATUS_ID);
    if (span && span.textContent && span.textContent.includes("Stopping")) {
      monitoring = true;
      watchStopStatus();
    }
  }

  // Dash renders and replaces components asynchronously. This listens only
  // for the confirmed stop label; it has no access to membership/action state.
  const observer = new MutationObserver(detectStopRequest);
  observer.observe(document.documentElement, {
    childList: true, characterData: true, subtree: true
  });
  document.addEventListener("DOMContentLoaded", detectStopRequest);
  detectStopRequest();
})();
