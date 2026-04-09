/**
 * Comelit Doorbell Card — Notification card for doorbell ring events.
 *
 * Idle:    Camera thumbnail with a doorbell icon overlay.
 * Ringing: Pulsing doorbell icon + "Answer" / "Dismiss" buttons.
 *          Auto-dismisses after `dismiss_after` seconds (default 30).
 * Answered: Live video stream with a stop button.
 *
 * Install:
 *   The Lovelace resource is registered automatically on HA startup.
 *
 *   Add card to dashboard (YAML):
 *     type: custom:comelit-doorbell-card
 *     doorbell_entity: event.comelit_intercom_doorbell
 *     camera_entity: camera.comelit_intercom_live_feed
 *     start_entity: button.comelit_intercom_start_video_feed
 *     stop_entity:  button.comelit_intercom_stop_video_feed
 *     dismiss_after: 30   # optional, seconds
 */
class ComelitDoorbellCard extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._config = null;
    this._state = "idle"; // idle | ringing | answered
    this._lastEventTs = null;
    this._dismissTimer = null;
    this._liveCard = null;
    this._onLocationChanged = null;
    this.attachShadow({ mode: "open" });
  }

  // ---------------------------------------------------------------------------
  // Lovelace lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    if (!config.doorbell_entity) {
      throw new Error("Missing required config: doorbell_entity");
    }
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._liveCard) this._liveCard.hass = hass;
    this._checkDoorbellState();
    if (this._state !== "answered") this._refreshThumbnail();
  }

  connectedCallback() {
    this._onLocationChanged = () => {
      setTimeout(() => {
        if (!this.isConnected || !this._isVisible()) this._dismiss();
      }, 0);
    };
    window.addEventListener("location-changed", this._onLocationChanged);
  }

  disconnectedCallback() {
    window.removeEventListener("location-changed", this._onLocationChanged);
    this._onLocationChanged = null;
    this._clearDismissTimer();
    if (this._state === "answered") this._callStop();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {
      doorbell_entity: "event.comelit_intercom_doorbell",
      camera_entity: "camera.comelit_intercom_live_feed",
      start_entity: "button.comelit_intercom_start_video_feed",
      stop_entity: "button.comelit_intercom_stop_video_feed",
      dismiss_after: 30,
    };
  }

  // ---------------------------------------------------------------------------
  // Doorbell state detection
  // ---------------------------------------------------------------------------

  _checkDoorbellState() {
    if (!this._hass || !this._config) return;
    const entity = this._hass.states[this._config.doorbell_entity];
    if (!entity) return;

    const lastChanged = entity.last_changed;
    if (lastChanged === this._lastEventTs) return;
    this._lastEventTs = lastChanged;

    if (entity.state !== "doorbell_ring") return;

    // Ignore stale events (older than dismiss_after) — e.g. on HA restart
    const dismissMs = (this._config.dismiss_after ?? 30) * 1000;
    const age = Date.now() - new Date(lastChanged).getTime();
    if (age > dismissMs) return;

    if (this._state !== "answered") this._showRinging();
  }

  // ---------------------------------------------------------------------------
  // State transitions
  // ---------------------------------------------------------------------------

  _showRinging() {
    this._state = "ringing";
    this._updateView();
    this._clearDismissTimer();
    const dismissMs = (this._config.dismiss_after ?? 30) * 1000;
    this._dismissTimer = setTimeout(() => this._dismiss(), dismissMs);
  }

  _dismiss() {
    this._clearDismissTimer();
    if (this._state === "answered") {
      this._callStop();
      this._teardownLiveCard();
    }
    this._state = "idle";
    this._updateView();
    this._refreshThumbnail();
  }

  async _answer() {
    this._clearDismissTimer();
    this._state = "answered";
    this._updateView();

    if (this._config.start_entity) {
      this._hass.callService("button", "press", {
        entity_id: this._config.start_entity,
      });
    }

    if (this._config.camera_entity) {
      const helpers = await window.loadCardHelpers();
      this._liveCard = await helpers.createCardElement({
        type: "picture-entity",
        entity: this._config.camera_entity,
        camera_view: "live",
        show_name: false,
        show_state: false,
      });
      this._liveCard.hass = this._hass;
      this.shadowRoot.getElementById("stream-slot").appendChild(this._liveCard);
    }
  }

  _stopAndReturn() {
    this._callStop();
    this._teardownLiveCard();
    this._state = "idle";
    this._updateView();
    this._refreshThumbnail();
  }

  _callStop() {
    if (this._hass && this._config?.stop_entity) {
      this._hass.callService("button", "press", {
        entity_id: this._config.stop_entity,
      });
    }
  }

  _teardownLiveCard() {
    const slot = this.shadowRoot.getElementById("stream-slot");
    if (slot) slot.innerHTML = "";
    this._liveCard = null;
  }

  _clearDismissTimer() {
    if (this._dismissTimer) {
      clearTimeout(this._dismissTimer);
      this._dismissTimer = null;
    }
  }

  // ---------------------------------------------------------------------------
  // View update
  // ---------------------------------------------------------------------------

  _updateView() {
    const idle = this.shadowRoot.getElementById("idle");
    const ringing = this.shadowRoot.getElementById("ringing");
    const live = this.shadowRoot.getElementById("live");
    if (!idle || !ringing || !live) return;
    idle.style.display = this._state === "idle" ? "" : "none";
    ringing.style.display = this._state === "ringing" ? "" : "none";
    live.style.display = this._state === "answered" ? "" : "none";
  }

  _refreshThumbnail() {
    if (!this._hass || !this._config?.camera_entity) return;
    const state = this._hass.states[this._config.camera_entity];
    const token = state?.attributes?.access_token;
    if (!token) return;
    const url = `/api/camera_proxy/${this._config.camera_entity}?token=${token}&t=${Date.now()}`;
    ["thumbnail", "thumbnail-ring"].forEach((id) => {
      const img = this.shadowRoot.getElementById(id);
      if (img) img.src = url;
    });
  }

  _isVisible() {
    const rect = this.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }

        /* Shared thumbnail container */
        .view {
          position: relative;
          background: #111;
          aspect-ratio: 5 / 3;
          width: 100%;
        }
        .thumbnail {
          width: 100%; height: 100%;
          object-fit: cover; display: block;
        }

        /* Idle overlay — subtle doorbell badge */
        .idle-overlay {
          position: absolute;
          bottom: 0; left: 0; right: 0;
          padding: 8px 12px;
          background: linear-gradient(transparent, rgba(0,0,0,0.55));
          color: rgba(255,255,255,0.85);
          font-size: 12px;
          display: flex; align-items: center; gap: 6px;
        }
        .idle-overlay svg { flex-shrink: 0; }

        /* Ringing overlay */
        .ringing-overlay {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 16px;
          background: rgba(0, 0, 0, 0.52);
        }
        .ring-icon {
          width: 68px; height: 68px;
          border-radius: 50%;
          background: var(--primary-color, #03a9f4);
          display: flex; align-items: center; justify-content: center;
          animation: pulse 1.1s ease-in-out infinite;
        }
        .ring-icon svg { fill: #fff; width: 36px; height: 36px; }
        @keyframes pulse {
          0%, 100% {
            transform: scale(1);
            box-shadow: 0 0 0 0 rgba(3, 169, 244, 0.55);
          }
          50% {
            transform: scale(1.08);
            box-shadow: 0 0 0 14px rgba(3, 169, 244, 0);
          }
        }
        .ring-label {
          color: #fff;
          font-size: 16px; font-weight: 500;
          text-shadow: 0 1px 4px rgba(0, 0, 0, 0.8);
        }
        .ring-actions { display: flex; gap: 12px; }
        .btn {
          padding: 10px 26px;
          border: none; border-radius: 24px;
          font-size: 14px; font-weight: 500;
          cursor: pointer; transition: opacity 0.15s;
        }
        .btn:hover { opacity: 0.85; }
        .btn-answer { background: #4caf50; color: #fff; }
        .btn-dismiss {
          background: rgba(255, 255, 255, 0.15);
          color: #fff;
          border: 1px solid rgba(255, 255, 255, 0.45);
        }

        /* Live: stop button */
        .live { display: none; position: relative; }
        .stop-btn {
          position: absolute; top: 8px; right: 8px;
          width: 32px; height: 32px;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.55);
          border: none; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          z-index: 10; transition: background 0.15s;
        }
        .stop-btn:hover { background: rgba(180, 0, 0, 0.75); }
        .stop-btn svg { fill: #fff; width: 16px; height: 16px; }
      </style>

      <ha-card>
        <!-- Idle: thumbnail + subtle badge -->
        <div class="view" id="idle">
          <img class="thumbnail" id="thumbnail" />
          <div class="idle-overlay">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="rgba(255,255,255,0.85)">
              <path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/>
            </svg>
            Doorbell
          </div>
        </div>

        <!-- Ringing: thumbnail + pulsing icon + actions -->
        <div class="view" id="ringing" style="display:none">
          <img class="thumbnail" id="thumbnail-ring" />
          <div class="ringing-overlay">
            <div class="ring-icon">
              <svg viewBox="0 0 24 24">
                <path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/>
              </svg>
            </div>
            <div class="ring-label">Someone at the door</div>
            <div class="ring-actions">
              <button class="btn btn-answer" id="answer-btn">Answer</button>
              <button class="btn btn-dismiss" id="dismiss-btn">Dismiss</button>
            </div>
          </div>
        </div>

        <!-- Live: stream + stop button -->
        <div class="live" id="live">
          <button class="stop-btn" id="stop-btn" title="Stop video">
            <svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>
          </button>
          <div id="stream-slot"></div>
        </div>
      </ha-card>
    `;

    this.shadowRoot
      .getElementById("answer-btn")
      .addEventListener("click", () => this._answer());
    this.shadowRoot
      .getElementById("dismiss-btn")
      .addEventListener("click", () => this._dismiss());
    this.shadowRoot
      .getElementById("stop-btn")
      .addEventListener("click", () => this._stopAndReturn());
  }
}

customElements.define("comelit-doorbell-card", ComelitDoorbellCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "comelit-doorbell-card",
  name: "Comelit Doorbell",
  description:
    "Doorbell notification card — shows ringing alert with Answer/Dismiss when someone rings.",
});
